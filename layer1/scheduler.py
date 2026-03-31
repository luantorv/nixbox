import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from layer1.database import get_engine
from layer1.models import Interaction, Recurrence, Task, TaskStatus

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

# ---------------------------------------------------------------------------
# Lanzamiento de tareas
# ---------------------------------------------------------------------------

async def _launch_task(task_id: int) -> None:
    """
    Carga la tarea desde la DB y la lanza en el sandbox.
    Es el punto de entrada tanto para tareas programadas como recurrentes.
    """
    from sqlmodel.ext.asyncio.session import AsyncSession

    from layer1.sandbox import run_task

    async with AsyncSession(get_engine(), expire_on_commit=False) as session:
        task = await session.get(Task, task_id)
        if task is None:
            logger.error("Tarea %d no encontrada al intentar lanzarla", task_id)
            return
        if task.status == TaskStatus.running:
            logger.warning("Tarea %d ya está en ejecución, se omite", task_id)
            return

        # Cargar interacciones antes de pasar al sandbox
        result = await session.exec(
            select(Interaction)
            .where(Interaction.task_id == task_id)
            .order_by(Interaction.created_at)
        )
        task.interactions = result.all()

        await run_task(task, session)

async def _launch_recurrent(recurrence_id: int) -> None:
    """
    Crea una nueva tarea a partir de la configuración de una recurrencia
    y la lanza. Actualiza last_execution y next_execution en la recurrencia.
    """
    async with AsyncSession(get_engine(), expire_on_commit=False) as session:
        recurrence = await session.get(Recurrence, recurrence_id)
        if recurrence is None or not recurrence.enabled:
            return

        # Buscar la tarea más reciente asociada a esta recurrencia como plantilla
        result = await session.exec(
            select(Task)
            .where(Task.recurrence_id == recurrence_id)
            .order_by(Task.created_at.desc())
        )
        template = result.first()
        if template is None:
            logger.error(
                "No hay tarea plantilla para la recurrencia %d", recurrence_id
            )
            return

        # Crear nueva tarea basada en la plantilla
        new_task = Task(
            name=template.name,
            sandbox_type=template.sandbox_type,
            recurrence_id=recurrence_id,
            status=TaskStatus.pending,
        )
        session.add(new_task)
        await session.flush()  # obtener new_task.id antes de commit

        # Copiar solo el prompt inicial (primer interaction con role="user")
        first_interaction_result = await session.exec(
            select(Interaction)
            .where(
                Interaction.task_id == template.id,
                Interaction.role == "user",
            )
            .order_by(Interaction.created_at)
        )
        first_interaction = first_interaction_result.first()
        if first_interaction is not None:
            session.add(Interaction(
                task_id=new_task.id,
                role="user",
                content=first_interaction.content,
            ))

        now = datetime.utcnow()
        recurrence.last_execution = now

        job = scheduler.get_job(f"recurrence-{recurrence_id}")
        if job is not None:
            next_run = job.next_run_time
            recurrence.next_execution = next_run

        session.add(recurrence)
        await session.commit()

        new_task.interactions = [first_interaction] if first_interaction else []
        from layer1.sandbox import run_task
        await run_task(new_task, session)

# ---------------------------------------------------------------------------
# Registro y cancelación de jobs
# ---------------------------------------------------------------------------

def schedule_once(task_id: int, run_at: datetime) -> None:
    """Programa una tarea para ejecutarse una única vez en run_at."""
    scheduler.add_job(
        _launch_task,
        trigger="date",
        run_date=run_at,
        args=[task_id],
        id=f"task-{task_id}",
        replace_existing=True,
    )
    logger.info("Tarea %d programada para %s", task_id, run_at)

def schedule_recurrence(recurrence_id: int, cron_string: str) -> None:
    """Registra un job recurrente usando una cron string."""
    scheduler.add_job(
        _launch_recurrent,
        trigger=CronTrigger.from_crontab(cron_string),
        args=[recurrence_id],
        id=f"recurrence-{recurrence_id}",
        replace_existing=True,
    )
    logger.info("Recurrencia %d registrada con cron '%s'", recurrence_id, cron_string)

def unschedule_recurrence(recurrence_id: int) -> None:
    """Elimina el job de una recurrencia sin borrarla de la DB."""
    job_id = f"recurrence-{recurrence_id}"
    if scheduler.get_job(job_id) is not None:
        scheduler.remove_job(job_id)
        logger.info("Recurrencia %d eliminada del scheduler", recurrence_id)

def unschedule_task(task_id: int) -> None:
    """Cancela una ejecución única programada."""
    job_id = f"task-{task_id}"
    if scheduler.get_job(job_id) is not None:
        scheduler.remove_job(job_id)
        logger.info("Tarea %d eliminada del scheduler", task_id)

# ---------------------------------------------------------------------------
# Inicialización
# ---------------------------------------------------------------------------

async def load_from_db() -> None:
    """
    Al arrancar el servicio, recarga desde la DB todas las recurrencias
    activas y las tareas con scheduled_at en el futuro.
    """
    async with AsyncSession(get_engine(), expire_on_commit=False) as session:
        recurrences_result = await session.exec(
            select(Recurrence).where(Recurrence.enabled == True)
        )
        for recurrence in recurrences_result.all():
            try:
                schedule_recurrence(recurrence.id, recurrence.cron_string)
            except Exception as exc:
                logger.error(
                    "Error al recargar recurrencia %d: %s", recurrence.id, exc
                )

        now = datetime.utcnow()
        tasks_result = await session.exec(
            select(Task).where(
                Task.scheduled_at != None,
                Task.scheduled_at > now,
                Task.status == TaskStatus.pending,
            )
        )
        for task in tasks_result.all():
            try:
                schedule_once(task.id, task.scheduled_at)
            except Exception as exc:
                logger.error(
                    "Error al recargar tarea programada %d: %s", task.id, exc
                )

    logger.info("Scheduler inicializado desde DB")