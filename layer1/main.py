import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from nixbox.config import settings
from nixbox.database import get_engine, get_session, init_db
from nixbox.models import (
    Interaction,
    InteractionCreate,
    LogEntry,
    Recurrence,
    RecurrenceCreate,
    Task,
    TaskCreate,
    TaskStatus,
)
from nixbox.sandbox import cancel_task
from nixbox.scheduler import (
    load_from_db,
    schedule_once,
    schedule_recurrence,
    scheduler,
    unschedule_recurrence,
    unschedule_task,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler.start()
    await load_from_db()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="nixbox", lifespan=lifespan)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

def run() -> None:
    import uvicorn
    uvicorn.run(
        "nixbox.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_task_or_404(task_id: int, session: AsyncSession) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return task

def _server_stats() -> dict:
    import shutil
    import psutil
    disk = shutil.disk_usage(settings.data_dir)
    return {
        "cpu_percent": psutil.cpu_percent(),
        "memory": psutil.virtual_memory()._asdict(),
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
        },
        "load_avg": psutil.getloadavg(),
    }

# ---------------------------------------------------------------------------
# / — Panel principal
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    stats = _server_stats()
    return templates.TemplateResponse(
        "index.html", {"request": request, "stats": stats}
    )

@app.get("/api/stats")
async def api_stats():
    return _server_stats()

# ---------------------------------------------------------------------------
# /tasks
# ---------------------------------------------------------------------------

@app.get("/tasks", response_class=HTMLResponse)
async def tasks_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    running_result = await session.exec(
        select(Task)
        .where(Task.status == TaskStatus.running)
        .order_by(Task.created_at.desc())
    )
    other_result = await session.exec(
        select(Task)
        .where(Task.status != TaskStatus.running)
        .order_by(Task.created_at.desc())
    )
    return templates.TemplateResponse("tasks/list.html", {
        "request": request,
        "running": running_result.all(),
        "other": other_result.all(),
    })

@app.get("/tasks/new", response_class=HTMLResponse)
async def tasks_new_form(request: Request):
    return templates.TemplateResponse("tasks/new.html", {
        "request": request,
        "sandbox_types": list(settings.sandbox_bins.keys()),
    })

@app.post("/tasks/new")
async def tasks_create(
    request: Request,
    name: str = Form(...),
    sandbox_type: str = Form(...),
    initial_prompt: str = Form(...),
    files: list[UploadFile] = [],
    session: AsyncSession = Depends(get_session),
):
    if sandbox_type not in settings.sandbox_bins:
        raise HTTPException(status_code=400, detail="Tipo de sandbox inválido")

    task = Task(name=name, sandbox_type=sandbox_type, status=TaskStatus.pending)
    session.add(task)
    await session.flush()

    # Guardar archivos de input
    inputs_dir = settings.inputs_dir(task.id)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    for upload in files:
        if upload.filename:
            dest = inputs_dir / upload.filename
            async with aiofiles.open(dest, "wb") as f:
                await f.write(await upload.read())

    # Guardar prompt inicial
    session.add(Interaction(
        task_id=task.id,
        role="user",
        content=initial_prompt,
    ))
    await session.commit()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)

@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(
    request: Request,
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    interactions_result = await session.exec(
        select(Interaction)
        .where(Interaction.task_id == task_id)
        .order_by(Interaction.created_at)
    )
    return templates.TemplateResponse("tasks/detail.html", {
        "request": request,
        "task": task,
        "interactions": interactions_result.all(),
    })

@app.post("/tasks/{task_id}/run")
async def task_run(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    if task.status == TaskStatus.running:
        raise HTTPException(status_code=409, detail="La tarea ya está en ejecución")

    interactions_result = await session.exec(
        select(Interaction)
        .where(Interaction.task_id == task_id)
        .order_by(Interaction.created_at)
    )
    task.interactions = interactions_result.all()

    from nixbox.sandbox import run_task
    asyncio.create_task(run_task(task, session))

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

@app.post("/tasks/{task_id}/stop")
async def task_stop(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    cancelled = await cancel_task(task_id, session)
    if not cancelled:
        raise HTTPException(status_code=409, detail="La tarea no está en ejecución")
    task.status = TaskStatus.cancelled
    task.pid = None
    session.add(task)
    await session.commit()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

@app.post("/tasks/{task_id}/delete")
async def task_delete(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    if task.status == TaskStatus.running:
        await cancel_task(task_id, session)
    unschedule_task(task_id)
    await session.delete(task)
    await session.commit()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/tasks", status_code=303)

@app.post("/tasks/{task_id}/prompt")
async def task_add_prompt(
    task_id: int,
    content: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    if task.status == TaskStatus.running:
        raise HTTPException(status_code=409, detail="La tarea está en ejecución")

    session.add(Interaction(task_id=task_id, role="user", content=content))
    await session.commit()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/tasks/{task_id}/run", status_code=303)

# ---------------------------------------------------------------------------
# /tasks/{task_id}/schedule
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/schedule", response_class=HTMLResponse)
async def task_schedule_form(
    request: Request,
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    recurrence = None
    if task.recurrence_id:
        recurrence = await session.get(Recurrence, task.recurrence_id)
    return templates.TemplateResponse("tasks/schedule.html", {
        "request": request,
        "task": task,
        "recurrence": recurrence,
    })

@app.post("/tasks/{task_id}/schedule")
async def task_schedule(
    task_id: int,
    mode: str = Form(...),           # "once" | "recurrent"
    scheduled_at: Optional[str] = Form(default=None),
    cron_string: Optional[str] = Form(default=None),
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)

    if mode == "once":
        if not scheduled_at:
            raise HTTPException(status_code=400, detail="scheduled_at requerido")
        run_at = datetime.fromisoformat(scheduled_at)
        task.scheduled_at = run_at
        task.recurrence_id = None
        session.add(task)
        await session.commit()
        schedule_once(task_id, run_at)

    elif mode == "recurrent":
        if not cron_string:
            raise HTTPException(status_code=400, detail="cron_string requerido")

        if task.recurrence_id:
            recurrence = await session.get(Recurrence, task.recurrence_id)
            recurrence.cron_string = cron_string
            recurrence.enabled = True
            session.add(recurrence)
            await session.commit()
            schedule_recurrence(recurrence.id, cron_string)
        else:
            recurrence = Recurrence(cron_string=cron_string, enabled=True)
            session.add(recurrence)
            await session.flush()
            task.recurrence_id = recurrence.id
            task.scheduled_at = None
            session.add(task)
            await session.commit()
            schedule_recurrence(recurrence.id, cron_string)

    else:
        raise HTTPException(status_code=400, detail="mode inválido")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

@app.post("/tasks/{task_id}/schedule/disable")
async def task_schedule_disable(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    if task.recurrence_id:
        recurrence = await session.get(Recurrence, task.recurrence_id)
        recurrence.enabled = False
        session.add(recurrence)
        await session.commit()
        unschedule_recurrence(recurrence.id)

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)

# ---------------------------------------------------------------------------
# /tasks/{task_id}/inputs y outputs
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/inputs", response_class=HTMLResponse)
async def task_inputs(
    request: Request,
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    await _get_task_or_404(task_id, session)
    inputs_dir = settings.inputs_dir(task_id)
    files = sorted(inputs_dir.iterdir()) if inputs_dir.exists() else []
    return templates.TemplateResponse("tasks/files.html", {
        "request": request,
        "task_id": task_id,
        "section": "inputs",
        "files": [f.name for f in files if f.is_file()],
    })

@app.get("/tasks/{task_id}/inputs/{filename}")
async def task_input_download(
    task_id: int,
    filename: str,
    session: AsyncSession = Depends(get_session),
):
    await _get_task_or_404(task_id, session)
    path = settings.inputs_dir(task_id) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path, filename=filename)

@app.get("/tasks/{task_id}/outputs", response_class=HTMLResponse)
async def task_outputs(
    request: Request,
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    outputs_dir = settings.outputs_dir(task_id)
    files = sorted(outputs_dir.iterdir()) if outputs_dir.exists() else []
    return templates.TemplateResponse("tasks/files.html", {
        "request": request,
        "task_id": task_id,
        "section": "outputs",
        "files": [f.name for f in files if f.is_file()],
        "task_running": task.status == TaskStatus.running,
    })

@app.get("/tasks/{task_id}/outputs/{filename}")
async def task_output_download(
    task_id: int,
    filename: str,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    if task.status == TaskStatus.running:
        raise HTTPException(status_code=409, detail="La tarea está en ejecución")
    path = settings.outputs_dir(task_id) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path, filename=filename)

# ---------------------------------------------------------------------------
# Descarga zip de inputs/outputs completos
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/inputs/download-all")
async def task_inputs_download_all(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    await _get_task_or_404(task_id, session)
    return await _zip_directory(settings.inputs_dir(task_id), f"inputs-{task_id}.zip")

@app.get("/tasks/{task_id}/outputs/download-all")
async def task_outputs_download_all(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    if task.status == TaskStatus.running:
        raise HTTPException(status_code=409, detail="La tarea está en ejecución")
    return await _zip_directory(settings.outputs_dir(task_id), f"outputs-{task_id}.zip")

async def _zip_directory(directory: Path, zip_name: str) -> StreamingResponse:
    if not directory.exists() or not any(directory.iterdir()):
        raise HTTPException(status_code=404, detail="No hay archivos")

    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in directory.iterdir():
            if file.is_file():
                zf.write(file, file.name)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={zip_name}"},
    )

# ---------------------------------------------------------------------------
# /tasks/{task_id}/logs y /logs/stream
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/inputs/download-all")
async def task_inputs_download_all(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    await _get_task_or_404(task_id, session)
    return _zip_response(settings.inputs_dir(task_id), f"task-{task_id}-inputs.zip")


@app.get("/tasks/{task_id}/outputs/download-all")
async def task_outputs_download_all(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    if task.status == TaskStatus.running:
        raise HTTPException(status_code=409, detail="La tarea está en ejecución")
    return _zip_response(settings.outputs_dir(task_id), f"task-{task_id}-outputs.zip")


def _zip_response(directory: Path, filename: str):
    import io
    import zipfile

    if not directory.exists():
        raise HTTPException(status_code=404, detail="No hay archivos")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in directory.iterdir():
            if f.is_file():
                zf.write(f, f.name)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

@app.get("/tasks/{task_id}/logs", response_class=HTMLResponse)
async def task_logs(
    request: Request,
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    result = await session.exec(
        select(LogEntry)
        .where(LogEntry.task_id == task_id)
        .order_by(LogEntry.id)
    )
    return templates.TemplateResponse("tasks/logs.html", {
        "request": request,
        "task": task,
        "entries": result.all(),
    })

@app.get("/tasks/{task_id}/logs/stream", response_class=HTMLResponse)
async def task_logs_stream_page(
    request: Request,
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    return templates.TemplateResponse("tasks/logs_stream.html", {
        "request": request,
        "task": task,
    })

@app.get("/tasks/{task_id}/logs/stream/sse")
async def task_logs_stream_sse(
    task_id: int,
    last_id: int = 0,
    session: AsyncSession = Depends(get_session),
):
    await _get_task_or_404(task_id, session)

    async def event_generator():
        seen_id = last_id
        while True:
            async with AsyncSession(get_engine(), expire_on_commit=False) as s:
                result = await s.exec(
                    select(LogEntry)
                    .where(
                        LogEntry.task_id == task_id,
                        LogEntry.id > seen_id,
                    )
                    .order_by(LogEntry.id)
                )
                entries = result.all()

            for entry in entries:
                seen_id = entry.id
                data = entry.content.replace("\n", "\\n")
                yield f"id: {entry.id}\ndata: [{entry.stream}] {data}\n\n"

            if not entries:
                yield ": keepalive\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )