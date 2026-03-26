from __future__ import annotations

import os

from .base import get_provider, register
from .anthropic import init as init_anthropic
from .openai import init as init_openai
from .google import init as init_google


def init_all_providers(token_file: str) -> None:
    """
    Lee el archivo de tokens en formato KEY=VALUE e inicializa
    los proveedores para los que haya una clave disponible.
    """
    tokens: dict[str, str] = {}
    try:
        with open(token_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    tokens[key.strip()] = value.strip()
    except OSError as exc:
        raise RuntimeError(f"No se pudo leer el archivo de tokens: {exc}") from exc

    if key := tokens.get("ANTHROPIC_API_KEY"):
        init_anthropic(key)

    if key := tokens.get("OPENAI_API_KEY"):
        init_openai(key)

    if key := tokens.get("GOOGLE_API_KEY"):
        init_google(key)


__all__ = ["get_provider", "init_all_providers"]
