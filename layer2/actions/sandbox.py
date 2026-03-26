from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from layer2.providers.base import ToolParam
from layer2.profile import SandboxProfile
from .base import Action, ActionResult, register

# Límites por defecto aplicados en el sandbox
_DEFAULT_TIMEOUT_SECS = 30
_DEFAULT_MEM_MB = 256
_MAX_OUTPUT_CHARS = 16_000

# Binarios requeridos
_BWRAP = shutil.which("bwrap")
_PYTHON = shutil.which("python3.12")
_NODE = shutil.which("node")


def _bwrap_cmd(
    workdir: Path,
    extra_bind_ro: list[Path] | None = None,
) -> list[str]:
    """
    Construye el prefijo de bubblewrap para un proceso sandboxeado.
    - Sin acceso a red (--unshare-net)
    - Sin acceso a /home ni /root
    - tmpfs en /tmp
    - workdir montado como rw
    - /nix/store montado ro (necesario para los intérpretes en NixOS)
    """
    cmd = [
        _BWRAP,
        "--unshare-all",
        "--share-net",          # la red se corta a nivel perfil, no aquí
        "--die-with-parent",
        # Sistema de archivos mínimo
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind", "/nix/store", "/nix/store",
        "--tmpfs", "/tmp",
        "--dev", "/dev",
        "--proc", "/proc",
        # Workdir propio de esta ejecución
        "--bind", str(workdir), "/sandbox",
        "--chdir", "/sandbox",
        # Sin home
        "--tmpfs", "/root",
        "--tmpfs", "/home",
    ]
    # Binds adicionales de solo lectura (ej. inputs)
    for p in (extra_bind_ro or []):
        cmd += ["--ro-bind", str(p), str(p)]

    return cmd


async def _run_sandboxed(
    cmd: list[str],
    workdir: Path,
    timeout: int,
    mem_mb: int,
    extra_bind_ro: list[Path] | None = None,
) -> ActionResult:
    if _BWRAP is None:
        return ActionResult.error("bubblewrap (bwrap) no está disponible en el sistema")

    full_cmd = _bwrap_cmd(workdir, extra_bind_ro) + cmd

    # systemd-run limita la memoria si está disponible
    systemd_run = shutil.which("systemd-run")
    if systemd_run:
        full_cmd = [
            systemd_run,
            "--scope",
            "--quiet",
            f"--property=MemoryMax={mem_mb}M",
            f"--property=MemorySwapMax=0",
            "--",
        ] + full_cmd

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return ActionResult.error(
                f"Tiempo de ejecución superado ({timeout}s)"
            )
    except OSError as exc:
        return ActionResult.error(f"Error al lanzar el proceso: {exc}")

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    combined = (out + ("\n[stderr]\n" + err if err.strip() else ""))[:_MAX_OUTPUT_CHARS]

    if proc.returncode != 0:
        return ActionResult.error(
            f"El proceso terminó con código {proc.returncode}:\n{combined}"
        )
    return ActionResult.ok(combined or "(sin output)")


# ---------------------------------------------------------------------------
# run_code
# ---------------------------------------------------------------------------

class RunCodeAction:
    name = "run_code"
    description = (
        "Ejecuta un fragmento de código en un sandbox aislado (sin red, sin acceso "
        "al sistema de archivos del host). Lenguajes soportados: python, javascript."
    )

    def __init__(self, profile: SandboxProfile, workdir_base: Path) -> None:
        self._profile = profile
        self._workdir_base = workdir_base

    def tool_param(self) -> ToolParam:
        return ToolParam(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": list(self._profile.allowed_languages),
                        "description": "Lenguaje del fragmento de código.",
                    },
                    "code": {
                        "type": "string",
                        "description": "Código a ejecutar.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Timeout en segundos (máximo {_DEFAULT_TIMEOUT_SECS}).",
                        "default": _DEFAULT_TIMEOUT_SECS,
                    },
                },
                "required": ["language", "code"],
            },
        )

    async def run(self, arguments: dict[str, Any]) -> ActionResult:
        language = arguments.get("language", "")
        code = arguments.get("code", "")
        timeout = min(int(arguments.get("timeout", _DEFAULT_TIMEOUT_SECS)), _DEFAULT_TIMEOUT_SECS)

        if not self._profile.allows_language(language):
            return ActionResult.error(
                f"Lenguaje '{language}' no permitido en este perfil."
            )

        with tempfile.TemporaryDirectory(dir=self._workdir_base, prefix="run_") as tmpdir:
            workdir = Path(tmpdir)

            if language == "python":
                if _PYTHON is None:
                    return ActionResult.error("python3.12 no disponible")
                script = workdir / "script.py"
                script.write_text(code)
                cmd = [_PYTHON, "/sandbox/script.py"]

            elif language == "javascript":
                if _NODE is None:
                    return ActionResult.error("node no disponible")
                script = workdir / "script.js"
                script.write_text(code)
                cmd = [_NODE, "/sandbox/script.js"]

            else:
                return ActionResult.error(f"Lenguaje no soportado: '{language}'")

            return await _run_sandboxed(
                cmd=cmd,
                workdir=workdir,
                timeout=timeout,
                mem_mb=_DEFAULT_MEM_MB,
            )


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------

# Comandos de shell permitidos explícitamente
_ALLOWED_SHELL_CMDS = frozenset({
    "ls", "cat", "echo", "grep", "find", "wc", "sort",
    "head", "tail", "cut", "tr", "sed", "awk", "diff",
    "unzip", "tar", "jq", "curl",
})


class ShellAction:
    name = "shell"
    description = (
        "Ejecuta un comando de shell en un sandbox aislado. "
        "Solo se permiten comandos de la lista blanca. "
        "Sin acceso a red ni al sistema de archivos del host."
    )

    def __init__(self, workdir_base: Path) -> None:
        self._workdir_base = workdir_base

    def tool_param(self) -> ToolParam:
        return ToolParam(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Comando a ejecutar. El primer token debe ser uno de: "
                            + ", ".join(sorted(_ALLOWED_SHELL_CMDS))
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Timeout en segundos (máximo {_DEFAULT_TIMEOUT_SECS}).",
                        "default": _DEFAULT_TIMEOUT_SECS,
                    },
                },
                "required": ["command"],
            },
        )

    async def run(self, arguments: dict[str, Any]) -> ActionResult:
        command = arguments.get("command", "").strip()
        timeout = min(int(arguments.get("timeout", _DEFAULT_TIMEOUT_SECS)), _DEFAULT_TIMEOUT_SECS)

        if not command:
            return ActionResult.error("Comando vacío")

        parts = command.split()
        base_cmd = os.path.basename(parts[0])
        if base_cmd not in _ALLOWED_SHELL_CMDS:
            return ActionResult.error(
                f"Comando '{base_cmd}' no permitido. "
                f"Permitidos: {sorted(_ALLOWED_SHELL_CMDS)}"
            )

        with tempfile.TemporaryDirectory(dir=self._workdir_base, prefix="shell_") as tmpdir:
            workdir = Path(tmpdir)
            return await _run_sandboxed(
                cmd=parts,
                workdir=workdir,
                timeout=timeout,
                mem_mb=_DEFAULT_MEM_MB,
            )


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

def init(profile: SandboxProfile, workdir_base: Path) -> None:
    register(RunCodeAction(profile, workdir_base))
    register(ShellAction(workdir_base))
