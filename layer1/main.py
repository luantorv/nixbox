from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from layer1.config import settings
from layer1.database import get_engine, get_session, init_db
from layer1.models import (
    Interaction,
    InteractionPhase,
    LogEntry,
    Recurrence,
    Task,
    TaskStatus,
)
from layer1.sandbox import approve_plan, cancel_task, register_active, revise_plan, run_task
from layer1.scheduler import (
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
        "layer1.main:app",
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
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
        "load_avg": psutil.getloadavg(),
    }


def _zip_response(directory: Path, filename: str) -> StreamingResponse:
    if not directory.exists() or not any(p for p in directory.iterdir() if p.is_file()):
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


# ---------------------------------------------------------------------------
# / — Panel principal
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html", {"request": request, "stats": _server_stats()}
    )


@app.get("/api/stats")
async def api_stats():
    return _server_stats()


# ---------------------------------------------------------------------------
# /tasks
# ---------------------------------------------------------------------------

@app.get("/tasks", response_class=HTMLResponse)
async def tasks_list(request: Request, session: AsyncSession = Depends(get_session)):
    active_statuses = [
        TaskStatus.planning,
        TaskStatus.awaiting_approval,
        TaskStatus.running,
    ]
    active_result = await session.exec(
        select(Task)
        .where(Task.status.in_(active_statuses))
        .order_by(Task.created_at.desc())
    )
    other_result = await session.exec(
        select(Task)
        .where(Task.status.not_in(active_statuses))
        .order_by(Task.created_at.desc())
    )
    return templates.TemplateResponse("tasks/list.html", {
        "request": request,
        "active": active_result.all(),
        "other": other_result.all(),
    })


@app.get("/tasks/new", response_class=HTMLResponse)
async def tasks_new_form(request: Request):
    return templates.TemplateResponse("tasks/new.html", {
        "request": request,
        "sandbox_types": list(settings.sandbox_profiles.keys()),
    })


@app.post("/tasks/new")
async def tasks_create(
    name: str = Form(...),
    sandbox_type: str = Form(...),
    initial_prompt: str = Form(...),
    files: list[UploadFile] = [],
    session: AsyncSession = Depends(get_session),
):
    if sandbox_type not in settings.sandbox_profiles:
        raise HTTPException(status_code=400, detail="Tipo de sandbox inválido")

    task = Task(name=name, sandbox_type=sandbox_type, status=TaskStatus.pending)
    session.add(task)
    await session.flush()

    inputs_dir = settings.inputs_dir(task.id)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    for upload in files:
        if upload.filename:
            async with aiofiles.open(inputs_dir / upload.filename, "wb") as f:
                await f.write(await upload.read())

    session.add(Interaction(
        task_id=task.id,
        role="user",
        content=initial_prompt,
        phase=InteractionPhase.planning,
    ))
    await session.commit()

    return RedirectResponse(url=f"/tasks/{task.id}", status_code=303)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(
    request: Request,
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)

    planning_result = await session.exec(
        select(Interaction)
        .where(
            Interaction.task_id == task_id,
            Interaction.phase == InteractionPhase.planning,
        )
        .order_by(Interaction.created_at)
    )
    execution_result = await session.exec(
        select(Interaction)
        .where(
            Interaction.task_id == task_id,
            Interaction.phase == InteractionPhase.execution,
        )
        .order_by(Interaction.created_at)
    )

    planning = planning_result.all()
    current_plan = next(
        (i.content for i in reversed(planning) if i.role == "assistant"),
        None,
    )

    return templates.TemplateResponse("tasks/detail.html", {
        "request": request,
        "task": task,
        "planning": planning,
        "current_plan": current_plan,
        "execution": execution_result.all(),
    })


@app.post("/tasks/{task_id}/run")
async def task_run(task_id: int, session: AsyncSession = Depends(get_session)):
    task = await _get_task_or_404(task_id, session)
    if task.status not in (TaskStatus.pending, TaskStatus.completed, TaskStatus.failed):
        raise HTTPException(status_code=409, detail=f"No se puede ejecutar en estado '{task.status}'")

    task.status = TaskStatus.pending
    session.add(task)
    await session.commit()

    asyncio_task = asyncio.create_task(run_task(task, session))
    register_active(task_id, asyncio_task)

    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/approve")
async def task_approve(task_id: int, session: AsyncSession = Depends(get_session)):
    ok = await approve_plan(task_id, session)
    if not ok:
        raise HTTPException(status_code=409, detail="La tarea no está esperando aprobación")
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/revise")
async def task_revise(
    task_id: int,
    feedback: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    ok = await revise_plan(task_id, feedback, session)
    if not ok:
        raise HTTPException(status_code=409, detail="La tarea no está esperando aprobación")
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/stop")
async def task_stop(task_id: int, session: AsyncSession = Depends(get_session)):
    ok = await cancel_task(task_id, session)
    if not ok:
        raise HTTPException(status_code=409, detail="La tarea no está activa")
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/delete")
async def task_delete(task_id: int, session: AsyncSession = Depends(get_session)):
    task = await _get_task_or_404(task_id, session)
    await cancel_task(task_id, session)
    unschedule_task(task_id)
    await session.delete(task)
    await session.commit()
    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/tasks/{task_id}/prompt")
async def task_add_prompt(
    task_id: int,
    content: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    task = await _get_task_or_404(task_id, session)
    if task.status in (TaskStatus.planning, TaskStatus.awaiting_approval, TaskStatus.running):
        raise HTTPException(status_code=409, detail="La tarea está activa")
    session.add(Interaction(
        task_id=task_id,
        role="user",
        content=content,
        phase=InteractionPhase.planning,
    ))
    await session.commit()
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
    recurrence = await session.get(Recurrence, task.recurrence_id) if task.recurrence_id else None
    return templates.TemplateResponse("tasks/schedule.html", {
        "request": request,
        "task": task,
        "recurrence": recurrence,
    })


@app.post("/tasks/{task_id}/schedule")
async def task_schedule(
    task_id: int,
    mode: str = Form(...),
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
            rec = await session.get(Recurrence, task.recurrence_id)
            rec.cron_string = cron_string
            rec.enabled = True
            session.add(rec)
            await session.commit()
            schedule_recurrence(rec.id, cron_string)
        else:
            rec = Recurrence(cron_string=cron_string, enabled=True)
            session.add(rec)
            await session.flush()
            task.recurrence_id = rec.id
            task.scheduled_at = None
            session.add(task)
            await session.commit()
            schedule_recurrence(rec.id, cron_string)
    else:
        raise HTTPException(status_code=400, detail="mode inválido")

    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


@app.post("/tasks/{task_id}/schedule/disable")
async def task_schedule_disable(task_id: int, session: AsyncSession = Depends(get_session)):
    task = await _get_task_or_404(task_id, session)
    if task.recurrence_id:
        rec = await session.get(Recurrence, task.recurrence_id)
        rec.enabled = False
        session.add(rec)
        await session.commit()
        unschedule_recurrence(rec.id)
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=303)


# ---------------------------------------------------------------------------
# /tasks/{task_id}/inputs y outputs
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/inputs", response_class=HTMLResponse)
async def task_inputs(request: Request, task_id: int, session: AsyncSession = Depends(get_session)):
    await _get_task_or_404(task_id, session)
    d = settings.inputs_dir(task_id)
    files = [f.name for f in sorted(d.iterdir()) if f.is_file()] if d.exists() else []
    return templates.TemplateResponse("tasks/files.html", {
        "request": request, "task_id": task_id, "section": "inputs",
        "files": files, "task_running": False,
    })


@app.get("/tasks/{task_id}/inputs/download-all")
async def task_inputs_download_all(task_id: int, session: AsyncSession = Depends(get_session)):
    await _get_task_or_404(task_id, session)
    return _zip_response(settings.inputs_dir(task_id), f"inputs-{task_id}.zip")


@app.get("/tasks/{task_id}/inputs/{filename}")
async def task_input_download(task_id: int, filename: str, session: AsyncSession = Depends(get_session)):
    await _get_task_or_404(task_id, session)
    path = settings.inputs_dir(task_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path, filename=filename)


@app.get("/tasks/{task_id}/outputs", response_class=HTMLResponse)
async def task_outputs(request: Request, task_id: int, session: AsyncSession = Depends(get_session)):
    task = await _get_task_or_404(task_id, session)
    d = settings.outputs_dir(task_id)
    files = [f.name for f in sorted(d.iterdir()) if f.is_file()] if d.exists() else []
    return templates.TemplateResponse("tasks/files.html", {
        "request": request, "task_id": task_id, "section": "outputs",
        "files": files, "task_running": task.status == TaskStatus.running,
    })


@app.get("/tasks/{task_id}/outputs/download-all")
async def task_outputs_download_all(task_id: int, session: AsyncSession = Depends(get_session)):
    task = await _get_task_or_404(task_id, session)
    if task.status == TaskStatus.running:
        raise HTTPException(status_code=409, detail="La tarea está en ejecución")
    return _zip_response(settings.outputs_dir(task_id), f"outputs-{task_id}.zip")


@app.get("/tasks/{task_id}/outputs/{filename}")
async def task_output_download(task_id: int, filename: str, session: AsyncSession = Depends(get_session)):
    task = await _get_task_or_404(task_id, session)
    if task.status == TaskStatus.running:
        raise HTTPException(status_code=409, detail="La tarea está en ejecución")
    path = settings.outputs_dir(task_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path, filename=filename)


# ---------------------------------------------------------------------------
# /tasks/{task_id}/logs y /logs/stream
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/logs", response_class=HTMLResponse)
async def task_logs(request: Request, task_id: int, session: AsyncSession = Depends(get_session)):
    task = await _get_task_or_404(task_id, session)
    result = await session.exec(
        select(LogEntry).where(LogEntry.task_id == task_id).order_by(LogEntry.id)
    )
    return templates.TemplateResponse("tasks/logs.html", {
        "request": request, "task": task, "entries": result.all(),
    })


@app.get("/tasks/{task_id}/logs/stream", response_class=HTMLResponse)
async def task_logs_stream_page(
    request: Request, task_id: int, session: AsyncSession = Depends(get_session)
):
    task = await _get_task_or_404(task_id, session)
    return templates.TemplateResponse("tasks/logs_stream.html", {
        "request": request, "task": task,
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
                    .where(LogEntry.task_id == task_id, LogEntry.id > seen_id)
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
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
