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
    def sandbox_bins(self) -> dict[str, str]:
        """
        Parses NIXBOX_SANDBOX_BINS env var.
        Format: "name1:/nix/store/.../bin/name1,name2:/nix/store/.../bin/name2"
        Returns: {"name1": "/nix/store/.../bin/name1", ...}
        """
        raw = os.environ.get("NIXBOX_SANDBOX_BINS", "")
        if not raw:
            return {}
        result = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if ":" in entry:
                name, path = entry.split(":", 1)
                result[name.strip()] = path.strip()
        return result

    def tasks_dir(self, task_id: int) -> Path:
        return Path(self.data_dir) / "tasks" / str(task_id)

    def inputs_dir(self, task_id: int) -> Path:
        return self.tasks_dir(task_id) / "inputs"

    def outputs_dir(self, task_id: int) -> Path:
        return self.tasks_dir(task_id) / "outputs"


settings = Settings()
