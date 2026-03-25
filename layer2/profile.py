from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Tipos base
# ---------------------------------------------------------------------------

Provider = Literal["anthropic", "openai", "google"]

ActionName = Literal[
    "read_input",
    "write_output",
    "http_get",
    "run_code",
    "shell",
]

Language = Literal["python", "javascript"]


@dataclass(frozen=True)
class ModelConfig:
    provider: Provider
    model: str


@dataclass(frozen=True)
class SandboxProfile:
    """
    Configuración completa de un tipo de sandbox.
    Definida en NixOS y cargada por layer2 al iniciar cada tarea.
    """
    name: str
    orchestrator_model: ModelConfig
    executor_model: ModelConfig
    allowed_domains: tuple[str, ...]
    allowed_actions: tuple[ActionName, ...]
    allowed_languages: tuple[Language, ...] = field(default=("python", "javascript"))

    def allows_action(self, action: ActionName) -> bool:
        return action in self.allowed_actions

    def allows_domain(self, domain: str) -> bool:
        """
        Comprobación sufijo-compatible con la lógica de agent-sandbox.nix:
        "anthropic.com" captura "api.anthropic.com".
        """
        domain = domain.lower().lstrip("www.")
        return any(
            domain == allowed or domain.endswith("." + allowed)
            for allowed in self.allowed_domains
        )

    def allows_language(self, language: Language) -> bool:
        return language in self.allowed_languages


# ---------------------------------------------------------------------------
# Carga desde variables de entorno
# ---------------------------------------------------------------------------
#
# El módulo NixOS serializa los perfiles en NIXBOX_PROFILES como JSON y los
# pasa al proceso de layer1, que los inyecta al lanzar cada tarea de layer2.
# El formato es:
#
#   NIXBOX_PROFILES='{
#     "news": {
#       "orchestrator_model": {"provider": "google", "model": "gemini-2.0-flash"},
#       "executor_model":     {"provider": "google", "model": "gemini-2.0-flash"},
#       "allowed_domains":    ["google.com", "wikipedia.org"],
#       "allowed_actions":    ["http_get", "write_output"],
#       "allowed_languages":  []
#     },
#     ...
#   }'

import json
import os


def load_profiles() -> dict[str, SandboxProfile]:
    raw = os.environ.get("NIXBOX_PROFILES", "{}")
    try:
        data: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"NIXBOX_PROFILES no es JSON válido: {exc}") from exc

    profiles: dict[str, SandboxProfile] = {}
    for name, cfg in data.items():
        try:
            profiles[name] = SandboxProfile(
                name=name,
                orchestrator_model=ModelConfig(**cfg["orchestrator_model"]),
                executor_model=ModelConfig(**cfg["executor_model"]),
                allowed_domains=tuple(cfg.get("allowed_domains", [])),
                allowed_actions=tuple(cfg.get("allowed_actions", [])),
                allowed_languages=tuple(cfg.get("allowed_languages", ["python", "javascript"])),
            )
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"Perfil '{name}' mal formado: {exc}") from exc

    return profiles


# Instancia global; se inicializa una vez al arrancar el proceso.
_profiles: dict[str, SandboxProfile] | None = None


def get_profiles() -> dict[str, SandboxProfile]:
    global _profiles
    if _profiles is None:
        _profiles = load_profiles()
    return _profiles


def get_profile(name: str) -> SandboxProfile:
    profiles = get_profiles()
    if name not in profiles:
        raise KeyError(f"Perfil de sandbox desconocido: '{name}'")
    return profiles[name]
