from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from layer2.actions.base import get_action, tool_params
from layer2.orchestrator import OrchestrationSession
from layer2.profile import SandboxProfile
from layer2.providers.base import Message, ToolResult, get_provider

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 32  # techo de seguridad para el loop agente

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
Sos un agente ejecutor. Tu trabajo es llevar a cabo el plan que se te provee \
usando las herramientas disponibles.

Reglas:
- Seguí el plan paso a paso.
- Usá las herramientas cuando sea necesario para completar cada paso.
- Escribí los resultados finales en /outputs usando write_output.
- Cuando hayas completado todos los pasos, indicalo explícitamente con la frase \
"TAREA COMPLETADA" al final de tu respuesta.
- Si encontrás un error irrecuperable, explicá el problema claramente y terminá \
con la frase "TAREA FALLIDA: <motivo>".
- No inventes resultados. Si una herramienta falla, reportalo honestamente.
"""


# ---------------------------------------------------------------------------
# Eventos que el executor emite durante la ejecución
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    text        = "text"        # fragmento de texto del modelo
    tool_call   = "tool_call"   # el modelo invocó una herramienta
    tool_result = "tool_result" # resultado de ejecutar la herramienta
    completed   = "completed"   # tarea finalizada con éxito
    failed      = "failed"      # tarea fallida
    error       = "error"       # error interno del executor


@dataclass
class ExecutionEvent:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Estado de ejecución
# ---------------------------------------------------------------------------

@dataclass
class ExecutionState:
    profile: SandboxProfile
    messages: list[Message] = field(default_factory=list)
    iterations: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    completed: bool = False
    failed: bool = False
    failure_reason: str | None = None

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class Executor:
    def __init__(self, profile: SandboxProfile) -> None:
        self._profile = profile
        self._provider = get_provider(profile.executor_model.provider)
        self._model = profile.executor_model.model

    async def run(
        self,
        session: OrchestrationSession,
    ) -> AsyncGenerator[ExecutionEvent, None]:
        """
        Ejecuta el plan aprobado. Emite eventos a medida que avanza
        para que layer1 pueda persistirlos como LogEntry y streamearlos.
        """
        if not session.current_plan:
            yield ExecutionEvent(type=EventType.error, data={"message": "No hay plan aprobado"})
            return

        state = ExecutionState(profile=self._profile)

        # El executor recibe el plan como primer mensaje del usuario
        state.messages.append(Message(
            role="user",
            content=(
                "Ejecutá el siguiente plan:\n\n"
                + session.current_plan
            ),
        ))

        async for event in self._loop(state):
            yield event

    async def _loop(
        self,
        state: ExecutionState,
    ) -> AsyncGenerator[ExecutionEvent, None]:
        tools = tool_params(self._profile.allowed_actions)

        while state.iterations < _MAX_ITERATIONS:
            state.iterations += 1
            logger.debug("Iteración %d del executor", state.iterations)

            try:
                response = await self._provider.complete(
                    messages=state.messages,
                    model=self._model,
                    system=_SYSTEM_PROMPT,
                    tools=tools or None,
                )
            except Exception as exc:
                logger.error("Error en llamada al proveedor: %s", exc)
                yield ExecutionEvent(
                    type=EventType.error,
                    data={"message": f"Error al llamar al modelo: {exc}"},
                )
                return

            state.record_usage(response.input_tokens, response.output_tokens)
            state.messages.append(response.message)

            # Emitir texto si lo hay
            if response.message.content:
                yield ExecutionEvent(
                    type=EventType.text,
                    data={"content": response.message.content},
                )

                # Detectar finalización
                content = response.message.content
                if "TAREA COMPLETADA" in content:
                    state.completed = True
                    yield ExecutionEvent(type=EventType.completed, data={
                        "input_tokens": state.total_input_tokens,
                        "output_tokens": state.total_output_tokens,
                        "iterations": state.iterations,
                    })
                    return

                if "TAREA FALLIDA" in content:
                    state.failed = True
                    reason = content.split("TAREA FALLIDA", 1)[-1].strip(" :")
                    state.failure_reason = reason
                    yield ExecutionEvent(type=EventType.failed, data={
                        "reason": reason,
                        "input_tokens": state.total_input_tokens,
                        "output_tokens": state.total_output_tokens,
                        "iterations": state.iterations,
                    })
                    return

            # Procesar tool calls
            if response.message.tool_calls:
                tool_results: list[ToolResult] = []

                for tc in response.message.tool_calls:
                    yield ExecutionEvent(
                        type=EventType.tool_call,
                        data={
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments,
                        },
                    )

                    try:
                        action = get_action(tc.name)
                        result = await action.run(tc.arguments)
                    except KeyError:
                        result_content = f"Acción '{tc.name}' no disponible en este perfil."
                        is_error = True
                    except Exception as exc:
                        logger.error("Error ejecutando acción '%s': %s", tc.name, exc)
                        result_content = f"Error interno al ejecutar '{tc.name}': {exc}"
                        is_error = True
                    else:
                        result_content = result.content
                        is_error = result.is_error

                    yield ExecutionEvent(
                        type=EventType.tool_result,
                        data={
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": result_content,
                            "is_error": is_error,
                        },
                    )

                    tool_results.append(ToolResult(
                        tool_call_id=tc.id,
                        content=result_content,
                        is_error=is_error,
                    ))

                # Agregar resultados al historial para el próximo turno
                state.messages.append(Message(
                    role="tool",
                    tool_results=tool_results,
                ))
                continue

            # Si no hay tool calls ni señal de fin, el modelo terminó su turno
            if response.stop_reason == "end_turn":
                # El modelo terminó sin la frase explícita; lo tratamos como completado
                state.completed = True
                yield ExecutionEvent(type=EventType.completed, data={
                    "input_tokens": state.total_input_tokens,
                    "output_tokens": state.total_output_tokens,
                    "iterations": state.iterations,
                })
                return

        # Se alcanzó el techo de iteraciones
        yield ExecutionEvent(
            type=EventType.failed,
            data={
                "reason": f"Se alcanzó el límite de {_MAX_ITERATIONS} iteraciones",
                "input_tokens": state.total_input_tokens,
                "output_tokens": state.total_output_tokens,
                "iterations": state.iterations,
            },
        )
