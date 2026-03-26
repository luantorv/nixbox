from __future__ import annotations

from pathlib import Path

from layer2.profile import SandboxProfile
from .files import init as init_files
from .http import init as init_http
from .sandbox import init as init_sandbox
from .base import get_actions, tool_params

_FILES_ACTIONS = frozenset({"read_input", "write_output", "list_inputs"})
_HTTP_ACTIONS  = frozenset({"http_get"})
_SANDBOX_ACTIONS = frozenset({"run_code", "shell"})


def init_all_actions(
    profile: SandboxProfile,
    inputs_dir: Path,
    outputs_dir: Path,
    workdir_base: Path,
) -> None:
    """
    Registra solo las acciones habilitadas en el perfil.
    Las instancias se crean con los paths y el perfil concreto de la tarea.
    """
    enabled = set(profile.allowed_actions)

    if enabled & _FILES_ACTIONS:
        init_files(inputs_dir, outputs_dir)

    if enabled & _HTTP_ACTIONS:
        init_http(profile)

    if enabled & _SANDBOX_ACTIONS:
        init_sandbox(profile, workdir_base)


__all__ = ["init_all_actions", "get_actions", "tool_params"]
