from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from layer2.providers.base import ToolParam
from .base import Action, ActionResult, register


def _safe_path(base: Path, relative: str) -> Path | None:
    """
    Resuelve `relative` dentro de `base` y verifica que no escape
    del directorio raíz (path traversal).
    Devuelve None si el path es inválido.
    """
    try:
        resolved = (base / relative).resolve()
        base_resolved = base.resolve()
        resolved.relative_to(base_resolved)  # lanza ValueError si escapa
        return resolved
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# read_input
# ---------------------------------------------------------------------------

class ReadInputAction:
    name = "read_input"
    description = "Lee el contenido de un archivo del directorio de inputs."

    def __init__(self, inputs_dir: Path) -> None:
        self._dir = inputs_dir

    def tool_param(self) -> ToolParam:
        return ToolParam(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Ruta relativa al archivo dentro de /inputs.",
                    }
                },
                "required": ["path"],
            },
        )

    async def run(self, arguments: dict[str, Any]) -> ActionResult:
        relative = arguments.get("path", "")
        resolved = _safe_path(self._dir, relative)
        if resolved is None:
            return ActionResult.error(f"Path inválido: {relative!r}")
        if not resolved.exists():
            return ActionResult.error(f"Archivo no encontrado: {relative!r}")
        if not resolved.is_file():
            return ActionResult.error(f"No es un archivo: {relative!r}")
        try:
            content = resolved.read_text(errors="replace")
            return ActionResult.ok(content)
        except OSError as exc:
            return ActionResult.error(f"Error al leer el archivo: {exc}")


# ---------------------------------------------------------------------------
# write_output
# ---------------------------------------------------------------------------

class WriteOutputAction:
    name = "write_output"
    description = (
        "Escribe o sobreescribe un archivo en el directorio de outputs. "
        "Crea los directorios intermedios si no existen."
    )

    def __init__(self, outputs_dir: Path) -> None:
        self._dir = outputs_dir

    def tool_param(self) -> ToolParam:
        return ToolParam(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Ruta relativa al archivo dentro de /outputs.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Contenido a escribir en el archivo.",
                    },
                },
                "required": ["path", "content"],
            },
        )

    async def run(self, arguments: dict[str, Any]) -> ActionResult:
        relative = arguments.get("path", "")
        content = arguments.get("content", "")
        resolved = _safe_path(self._dir, relative)
        if resolved is None:
            return ActionResult.error(f"Path inválido: {relative!r}")
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content)
            return ActionResult.ok(f"Archivo escrito: {relative}")
        except OSError as exc:
            return ActionResult.error(f"Error al escribir el archivo: {exc}")


# ---------------------------------------------------------------------------
# list_inputs
# ---------------------------------------------------------------------------

class ListInputsAction:
    name = "list_inputs"
    description = "Lista los archivos disponibles en el directorio de inputs."

    def __init__(self, inputs_dir: Path) -> None:
        self._dir = inputs_dir

    def tool_param(self) -> ToolParam:
        return ToolParam(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def run(self, arguments: dict[str, Any]) -> ActionResult:
        if not self._dir.exists():
            return ActionResult.ok("El directorio de inputs está vacío.")
        files = [
            str(p.relative_to(self._dir))
            for p in sorted(self._dir.rglob("*"))
            if p.is_file()
        ]
        if not files:
            return ActionResult.ok("El directorio de inputs está vacío.")
        return ActionResult.ok("\n".join(files))


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

def init(inputs_dir: Path, outputs_dir: Path) -> None:
    register(ReadInputAction(inputs_dir))
    register(WriteOutputAction(outputs_dir))
    register(ListInputsAction(inputs_dir))
