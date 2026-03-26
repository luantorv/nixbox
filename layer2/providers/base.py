from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Tipos comunes
# ---------------------------------------------------------------------------

Role = Literal["user", "assistant", "tool"]


@dataclass
class ToolParam:
    """Definición de una herramienta que el modelo puede invocar."""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class ToolCall:
    """Invocación de herramienta solicitada por el modelo."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    """Resultado de ejecutar una herramienta."""
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


@dataclass
class CompletionResponse:
    message: Message
    input_tokens: int
    output_tokens: int
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop"]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class Provider(Protocol):
    async def complete(
        self,
        messages: list[Message],
        model: str,
        system: str | None = None,
        tools: list[ToolParam] | None = None,
        max_tokens: int = 4096,
    ) -> CompletionResponse:
        """
        Envía los mensajes al modelo y devuelve la respuesta.
        Si `tools` está presente, el modelo puede responder con tool_calls.
        """
        ...


# ---------------------------------------------------------------------------
# Registro de proveedores
# ---------------------------------------------------------------------------

_registry: dict[str, Provider] = {}


def register(name: str, provider: Provider) -> None:
    _registry[name] = provider


def get_provider(name: str) -> Provider:
    if name not in _registry:
        raise KeyError(f"Proveedor desconocido: '{name}'. Disponibles: {list(_registry)}")
    return _registry[name]
