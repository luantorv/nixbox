from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from layer2.providers.base import ToolParam


# ---------------------------------------------------------------------------
# Resultado de una acción
# ---------------------------------------------------------------------------

@dataclass
class ActionResult:
    content: str
    is_error: bool = False

    @classmethod
    def ok(cls, content: str) -> ActionResult:
        return cls(content=content, is_error=False)

    @classmethod
    def error(cls, message: str) -> ActionResult:
        return cls(content=message, is_error=True)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Action(Protocol):
    name: str
    description: str

    def tool_param(self) -> ToolParam:
        """Devuelve la definición de la herramienta para enviar al modelo."""
        ...

    async def run(self, arguments: dict[str, Any]) -> ActionResult:
        """Ejecuta la acción y devuelve el resultado."""
        ...


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

_registry: dict[str, Action] = {}


def register(action: Action) -> None:
    _registry[action.name] = action


def get_action(name: str) -> Action:
    if name not in _registry:
        raise KeyError(f"Acción desconocida: '{name}'. Disponibles: {list(_registry)}")
    return _registry[name]


def get_actions(names: tuple[str, ...]) -> list[Action]:
    """Devuelve las instancias de las acciones solicitadas, en el mismo orden."""
    return [get_action(n) for n in names]


def tool_params(names: tuple[str, ...]) -> list[ToolParam]:
    """Devuelve las definiciones de herramienta para enviar al modelo."""
    return [get_action(n).tool_param() for n in names]
