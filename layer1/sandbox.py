import asyncio
import logging
from datetime import datetime
from pathlib import Path

from sqlmodel.ext.asyncio.session import AsyncSession

from nixbox.config import settings
from nixbox.models import Interaction, LogEntry, LogStream, Task, TaskStatus

logger = logging.getLogger(__name__)

# Registro en memoria de procesos activos: task_id -> Process
_running: dict[int, asyncio.subprocess.Process] = {}


def _build_prompt(interactions: list[Interaction]) -> str:
    """
    Construye el prompt completo a partir del historial de interacciones.
    El agente recibe el historial completo en cada ejecución.
    """
    parts = []
    for interaction in sorted(interactions, key=lambda i: i.created_at):
        prefix = "User" if interaction.role == "user" else "Assistant"
        parts.append(f"{prefix}: {interaction.content}")
    return "\n\n".join(parts)


async def _stream_output(
    stream: asyncio.StreamReader,
    log_stream: LogStream,
    task_id: int,
    session: AsyncSession,
) -> None:
    """Lee líneas de un stream async y las persiste como LogEntry."""
    while True:
        try:
            line = await stream.readline()
        except Exception as exc:
            logger.warning("Error leyendo stream %s de tarea %d: %s", log_stream, task_id, exc)
            break
        if not line:
            break
        content = line.decode(errors="replace").rstrip("\n")
        entry = LogEntry(
            task_id=task_id,
            stream=log_stream,
            content=content,
            created_at=datetime.utcnow(),
        )
        session.add(entry)
        await session.commit()


async def run_task(task: Task, session: AsyncSession) -> None:
    """
    Lanza el sandbox para una tarea, captura su output y actualiza el estado.
    Se espera que las interacciones de la tarea ya estén cargadas en `task.interactions`.
    """
    sandbox_bin = settings.sandbox_bins.get(task.sandbox_type)
    if sandbox_bin is None:
        logger.error("Tipo de sandbox desconocido: %s", task.sandbox_type)
        task.status = TaskStatus.failed
        session.add(task)
        await session.commit()
        return

    inputs_dir = settings.inputs_dir(task.id)
    outputs_dir = settings.outputs_dir(task.id)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    prompt = _build_prompt(task.interactions)

    env = _build_env(task)

    try:
        process = await asyncio.create_subprocess_exec(
            sandbox_bin,
            "--print-output",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(outputs_dir),
            env=env,
        )
    except Exception as exc:
        logger.error("No se pudo lanzar el sandbox para tarea %d: %s", task.id, exc)
        task.status = TaskStatus.failed
        session.add(task)
        await session.commit()
        return

    task.status = TaskStatus.running
    task.pid = process.pid
    session.add(task)
    await session.commit()

    _running[task.id] = process

    # Enviar el prompt por stdin y cerrar
    try:
        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()
    except Exception as exc:
        logger.warning("Error escribiendo stdin para tarea %d: %s", task.id, exc)

    # Leer stdout y stderr concurrentemente
    await asyncio.gather(
        _stream_output(process.stdout, LogStream.stdout, task.id, session),
        _stream_output(process.stderr, LogStream.stderr, task.id, session),
    )

    returncode = await process.wait()
    _running.pop(task.id, None)

    task.pid = None
    task.status = TaskStatus.completed if returncode == 0 else TaskStatus.failed
    session.add(task)
    await session.commit()

    logger.info("Tarea %d finalizada con código %d", task.id, returncode)


async def cancel_task(task_id: int, session: AsyncSession) -> bool:
    """
    Envía SIGTERM al proceso del sandbox si está en ejecución.
    Devuelve True si se envió la señal, False si no había proceso activo.
    """
    process = _running.get(task_id)
    if process is None:
        return False
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    _running.pop(task_id, None)
    return True


def _build_env(task: Task) -> dict[str, str]:
    """
    Construye las variables de entorno para el subproceso del sandbox.
    Lee los tokens desde el archivo configurado en settings.
    """
    import os

    env = dict(os.environ)

    token_file = Path(settings.token_file)
    if token_file.exists():
        for line in token_file.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()

    # El sandbox monta outputs_dir como cwd, inputs como variable de entorno
    env["NIXBOX_INPUTS_DIR"] = str(settings.inputs_dir(task.id))

    return env
