from __future__ import annotations

import json
import os
from functools import cached_property
from pathlib import Path


class Settings:

    @cached_property
    def data_dir(self) -> str:
        return os.environ.get("NIXBOX_DATA_DIR", "/var/lib/nixbox")

    @cached_property
    def token_file(self) -> str:
        return os.environ.get("NIXBOX_TOKEN_FILE", "")

    @cached_property
    def host(self) -> str:
        return os.environ.get("NIXBOX_HOST", "127.0.0.1")

    @cached_property
    def port(self) -> int:
        return int(os.environ.get("NIXBOX_PORT", "8000"))

    @cached_property
    def sandbox_profiles(self) -> dict[str, object]:
        """
        Parsea NIXBOX_PROFILES y devuelve un dict con los nombres de perfil
        como claves. El valor es el dict crudo; layer2.profile.get_profile()
        es quien construye el SandboxProfile tipado a partir de él.

        Solo se usa en layer1 para validar que el sandbox_type de una tarea
        sea conocido, y para poblar el selector en /tasks/new.
        """
        raw = os.environ.get("NIXBOX_PROFILES", "{}")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {}
            return data
        except json.JSONDecodeError:
            return {}

    # -----------------------------------------------------------------------
    # Paths por tarea
    # -----------------------------------------------------------------------

    def tasks_dir(self, task_id: int) -> Path:
        return Path(self.data_dir) / "tasks" / str(task_id)

    def inputs_dir(self, task_id: int) -> Path:
        return self.tasks_dir(task_id) / "inputs"

    def outputs_dir(self, task_id: int) -> Path:
        return self.tasks_dir(task_id) / "outputs"

    def work_dir(self, task_id: int) -> Path:
        """Directorio temporal para ejecución de código en el sandbox."""
        return self.tasks_dir(task_id) / "work"


settings = Settings()
