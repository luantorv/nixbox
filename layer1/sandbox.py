from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from layer1.config import settings
from layer1.models import (
    Interaction,
    InteractionPhase,
    LogEntry,
    LogStream,
    Task,
    TaskStatus,
)

logger = logging.getLogger(__name__)

# Registro en memoria de tasks en fase de planificación o ejecución
# task_id -> asyncio.Task
_active: dict[int, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Punto de entrada principal
# ---------------------------------------------------------------------------

async def run_task(task: Task, session: AsyncSession) -> None:
    """
    Orquesta el ciclo completo de una tarea:
    1. Fase de planificación (Orchestrator)
    2. Espera aprobación del usuario (status = awaiting_approval)
    3. Fase de ejecución (Executor) una vez aprobado

    Este coroutine se lanza como asyncio.Task desde main.py para no bloquear.
    """
    from layer2.actions import init_all_actions
    from layer2.orchestrator import Orchestrator
    from layer2.profile import get_profile
    from layer2.providers import init_all_providers

    try:
        # Inicializar proveedores desde el token file
        init_all_providers(settings.token_file)

        profile = get_profile(task.sandbox_type)

        inputs_dir  = settings.inputs_dir(task.id)
        outputs_dir = settings.outputs_dir(task.id)
        workdir     = settings.work_dir(task.id)
        workdir.mkdir(parents=True, exist_ok=True)
        outputs_dir.mkdir(parents=True, exist_ok=True)

        # Registrar acciones para esta tarea
        init_all_actions(profile, inputs_dir, outputs_dir, workdir)

        # Cargar prompt inicial
        result = await session.exec(
            select(Interaction)
            .where(
                Interaction.task_id == task.id,
                Interaction.phase == InteractionPhase.planning,
                Interaction.role == "user",
            )
            .order_by(Interaction.created_at)
        )
        interactions = result.all()
        if not interactions:
            await _set_failed(task, session, "No hay prompt inicial")
            return

        initial_prompt = interactions[0].content

        # ---- Fase de planificación ----
        await _set_status(task, session, TaskStatus.planning)
        await _log(task.id, session, LogStream.system, "Generando plan...")

        orchestrator = Orchestrator(profile)

        # Reconstruir sesión del orquestador si hay revisiones previas
        orch_session = await orchestrator.start(initial_prompt)

        # Persistir plan inicial
        await _save_interaction(
            task.id, session,
            role="assistant",
            content=orch_session.current_plan,
            phase=InteractionPhase.planning,
        )
        await _log(task.id, session, LogStream.system, "Plan generado. Esperando aprobación.")
        await _set_status(task, session, TaskStatus.awaiting_approval)

        # El loop de revisión se maneja desde main.py mediante
        # approve_plan() y revise_plan(). Este coroutine queda suspendido
        # hasta que la tarea cambie de estado.
        while True:
            await asyncio.sleep(1)
            await session.refresh(task)

            if task.status == TaskStatus.awaiting_approval:
                continue

            if task.status == TaskStatus.cancelled:
                await _log(task.id, session, LogStream.system, "Tarea cancelada.")
                return

            if task.status == TaskStatus.running:
                # El usuario aprobó; buscar si hubo revisiones
                result = await session.exec(
                    select(Interaction)
                    .where(
                        Interaction.task_id == task.id,
                        Interaction.phase == InteractionPhase.planning,
                    )
                    .order_by(Interaction.created_at)
                )
                all_planning = result.all()

                # Reconstruir sesión del orquestador con el historial completo
                if len(all_planning) > 2:
                    orch_session = await _rebuild_orch_session(
                        orchestrator, all_planning
                    )

                break

            # Cualquier otro estado (failed, etc.) sale del loop
            return

        # ---- Fase de ejecución ----
        await _log(task.id, session, LogStream.system, "Iniciando ejecución del plan.")
        await _run_executor(task, session, orch_session)

    except asyncio.CancelledError:
        await _set_status(task, session, TaskStatus.cancelled)
        await _log(task.id, session, LogStream.system, "Tarea cancelada.")
    except Exception as exc:
        logger.exception("Error inesperado en tarea %d", task.id)
        await _set_failed(task, session, str(exc))
    finally:
        _active.pop(task.id, None)


# ---------------------------------------------------------------------------
# Ejecución del plan
# ---------------------------------------------------------------------------

async def _run_executor(
    task: Task,
    session: AsyncSession,
    orch_session,
) -> None:
    from layer2.executor import EventType, Executor

    executor = Executor(orch_session.profile)

    async for event in executor.run(orch_session):
        if event.type == EventType.text:
            await _log(task.id, session, LogStream.stdout, event.data["content"])

        elif event.type == EventType.tool_call:
            msg = (
                f"[tool_call] {event.data['name']}"
                f"({json.dumps(event.data['arguments'], ensure_ascii=False)})"
            )
            await _log(task.id, session, LogStream.system, msg)

        elif event.type == EventType.tool_result:
            status = "ERROR" if event.data["is_error"] else "OK"
            msg = f"[tool_result:{status}] {event.data['name']}: {event.data['content'][:500]}"
            await _log(task.id, session, LogStream.system, msg)

        elif event.type == EventType.completed:
            await _log(
                task.id, session, LogStream.system,
                f"Tarea completada. "
                f"Tokens: {event.data['input_tokens']} entrada / "
                f"{event.data['output_tokens']} salida. "
                f"Iteraciones: {event.data['iterations']}.",
            )
            task.status = TaskStatus.completed
            task.pid = None
            session.add(task)
            await session.commit()
            return

        elif event.type in (EventType.failed, EventType.error):
            reason = event.data.get("reason") or event.data.get("message", "Error desconocido")
            await _set_failed(task, session, reason)
            return


# ---------------------------------------------------------------------------
# Aprobación y revisión del plan desde main.py
# ---------------------------------------------------------------------------

async def approve_plan(task_id: int, session: AsyncSession) -> bool:
    """
    Llamado desde main.py cuando el usuario aprueba el plan.
    Cambia el status a `running` para que el loop en run_task avance.
    Devuelve False si la tarea no está en awaiting_approval.
    """
    task = await session.get(Task, task_id)
    if task is None or task.status != TaskStatus.awaiting_approval:
        return False
    task.status = TaskStatus.running
    session.add(task)
    await session.commit()
    return True


async def revise_plan(
    task_id: int,
    feedback: str,
    session: AsyncSession,
) -> bool:
    """
    Llamado desde main.py cuando el usuario pide cambios al plan.
    Persiste el feedback, llama al orquestador para revisar, y
    guarda el nuevo plan. La tarea sigue en awaiting_approval.
    Devuelve False si la tarea no está en awaiting_approval.
    """
    from layer2.orchestrator import Orchestrator
    from layer2.profile import get_profile
    from layer2.providers import init_all_providers

    task = await session.get(Task, task_id)
    if task is None or task.status != TaskStatus.awaiting_approval:
        return False

    init_all_providers(settings.token_file)
    profile = get_profile(task.sandbox_type)
    orchestrator = Orchestrator(profile)

    # Cargar historial de planificación
    result = await session.exec(
        select(Interaction)
        .where(
            Interaction.task_id == task_id,
            Interaction.phase == InteractionPhase.planning,
        )
        .order_by(Interaction.created_at)
    )
    planning_history = result.all()

    # Guardar feedback del usuario
    await _save_interaction(task_id, session, "user", feedback, InteractionPhase.planning)

    # Reconstruir sesión y revisar
    orch_session = await _rebuild_orch_session(orchestrator, planning_history)
    from layer2.providers.base import Message
    orch_session.history.append(Message(role="user", content=feedback))
    orch_session = await orchestrator.revise(orch_session, feedback)

    # Guardar plan revisado
    await _save_interaction(
        task_id, session,
        role="assistant",
        content=orch_session.current_plan,
        phase=InteractionPhase.planning,
    )
    await _log(task_id, session, LogStream.system, "Plan revisado. Esperando aprobación.")
    return True


async def cancel_task(task_id: int, session: AsyncSession) -> bool:
    """
    Cancela una tarea activa en cualquier fase.
    Devuelve True si había algo que cancelar.
    """
    task = await session.get(Task, task_id)
    if task is None:
        return False

    active = _active.get(task_id)
    if active is not None:
        active.cancel()

    task.status = TaskStatus.cancelled
    task.pid = None
    session.add(task)
    await session.commit()
    _active.pop(task_id, None)
    return True


def register_active(task_id: int, asyncio_task: asyncio.Task) -> None:
    _active[task_id] = asyncio_task


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

async def _rebuild_orch_session(orchestrator, planning_interactions):
    """
    Reconstruye una OrchestrationSession a partir del historial
    persistido en la DB, sin hacer llamadas al modelo.
    """
    from layer2.orchestrator import OrchestrationSession, OrchestrationStatus
    from layer2.providers.base import Message

    session = OrchestrationSession(profile=orchestrator._profile)
    for interaction in planning_interactions:
        session.history.append(Message(
            role=interaction.role,
            content=interaction.content,
        ))
    if planning_interactions:
        last_assistant = next(
            (i for i in reversed(planning_interactions) if i.role == "assistant"),
            None,
        )
        if last_assistant:
            session.current_plan = last_assistant.content
    return session


async def _set_status(task: Task, session: AsyncSession, status: TaskStatus) -> None:
    task.status = status
    session.add(task)
    await session.commit()


async def _set_failed(task: Task, session: AsyncSession, reason: str) -> None:
    task.status = TaskStatus.failed
    task.pid = None
    session.add(task)
    await session.commit()
    await _log(task.id, session, LogStream.system, f"FALLIDO: {reason}")


async def _log(
    task_id: int,
    session: AsyncSession,
    stream: LogStream,
    content: str,
) -> None:
    entry = LogEntry(
        task_id=task_id,
        stream=stream,
        content=content,
        created_at=datetime.utcnow(),
    )
    session.add(entry)
    await session.commit()


async def _save_interaction(
    task_id: int,
    session: AsyncSession,
    role: str,
    content: str,
    phase: InteractionPhase,
) -> None:
    session.add(Interaction(
        task_id=task_id,
        role=role,
        content=content,
        phase=phase,
        created_at=datetime.utcnow(),
    ))
    await session.commit()
