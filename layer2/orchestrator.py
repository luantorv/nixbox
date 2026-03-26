from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from layer2.profile import SandboxProfile
from layer2.providers.base import Message, get_provider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
Sos un orquestador de tareas. Tu única responsabilidad es analizar la solicitud \
del usuario y producir un plan de acción claro y detallado.

Reglas estrictas:
- Responde SIEMPRE con un plan en formato Markdown usando listas.
- NO ejecutes ninguna acción, NO escribas código, NO accedas a recursos externos.
- El plan debe ser lo suficientemente detallado para que otro agente pueda seguirlo \
sin ambigüedad.
- Si la solicitud es ambigua, incluí una sección "## Supuestos" con los supuestos \
que estás tomando.
- Usá ## para secciones y - o 1. para los pasos.

Estructura sugerida del plan:
## Objetivo
Una oración que resume la tarea.

## Pasos
1. Primer paso
2. Segundo paso
   - Sub-paso si es necesario

## Supuestos
- (si los hay)

## Herramientas necesarias
- Lista de herramientas que el agente ejecutor va a necesitar.
"""

_REVISION_PROMPT = """\
El usuario pidió cambios al plan anterior. Revisá el plan teniendo en cuenta \
sus comentarios y producí una versión actualizada completa. \
Mantené el mismo formato Markdown.\
"""

# ---------------------------------------------------------------------------
# Estado de la sesión de orquestación
# ---------------------------------------------------------------------------

class OrchestrationStatus(str, Enum):
    planning   = "planning"    # esperando aprobación del usuario
    approved   = "approved"    # plan aprobado, listo para ejecutar
    cancelled  = "cancelled"   # usuario canceló


@dataclass
class OrchestrationSession:
    profile: SandboxProfile
    status: OrchestrationStatus = OrchestrationStatus.planning
    current_plan: str | None = None
    history: list[Message] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------

class Orchestrator:
    def __init__(self, profile: SandboxProfile) -> None:
        self._profile = profile
        self._provider = get_provider(profile.orchestrator_model.provider)
        self._model = profile.orchestrator_model.model

    async def start(self, prompt: str) -> OrchestrationSession:
        """
        Recibe el prompt inicial del usuario y genera el primer plan.
        Devuelve la sesión con el plan en `current_plan`.
        """
        session = OrchestrationSession(profile=self._profile)

        session.history.append(Message(role="user", content=prompt))

        response = await self._provider.complete(
            messages=session.history,
            model=self._model,
            system=_SYSTEM_PROMPT,
        )
        session.record_usage(response.input_tokens, response.output_tokens)

        plan = response.message.content or ""
        session.current_plan = plan
        session.history.append(Message(role="assistant", content=plan))

        logger.info(
            "Plan inicial generado (%d tokens entrada, %d salida)",
            response.input_tokens,
            response.output_tokens,
        )
        return session

    async def revise(
        self,
        session: OrchestrationSession,
        feedback: str,
    ) -> OrchestrationSession:
        """
        El usuario pidió cambios. Agrega el feedback al historial
        y genera un plan revisado.
        """
        if session.status != OrchestrationStatus.planning:
            raise RuntimeError(
                f"No se puede revisar un plan en estado '{session.status}'"
            )

        session.history.append(Message(role="user", content=feedback))

        response = await self._provider.complete(
            messages=session.history,
            model=self._model,
            system=_SYSTEM_PROMPT + "\n\n" + _REVISION_PROMPT,
        )
        session.record_usage(response.input_tokens, response.output_tokens)

        plan = response.message.content or ""
        session.current_plan = plan
        session.history.append(Message(role="assistant", content=plan))

        logger.info(
            "Plan revisado (%d tokens entrada, %d salida)",
            response.input_tokens,
            response.output_tokens,
        )
        return session

    def approve(self, session: OrchestrationSession) -> OrchestrationSession:
        """Marca el plan como aprobado."""
        if session.status != OrchestrationStatus.planning:
            raise RuntimeError(
                f"No se puede aprobar un plan en estado '{session.status}'"
            )
        session.status = OrchestrationStatus.approved
        logger.info("Plan aprobado")
        return session

    def cancel(self, session: OrchestrationSession) -> OrchestrationSession:
        """Cancela la sesión de orquestación."""
        session.status = OrchestrationStatus.cancelled
        logger.info("Orquestación cancelada por el usuario")
        return session
