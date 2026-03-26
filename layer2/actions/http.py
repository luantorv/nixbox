from __future__ import annotations

from typing import Any

import httpx

from layer2.providers.base import ToolParam
from layer2.profile import SandboxProfile
from .base import Action, ActionResult, register


class HttpGetAction:
    name = "http_get"
    description = (
        "Realiza una solicitud HTTP GET a una URL. "
        "Solo se permiten dominios autorizados por el perfil del sandbox. "
        "No sigue redirecciones fuera del dominio original."
    )

    def __init__(self, profile: SandboxProfile) -> None:
        self._profile = profile

    def tool_param(self) -> ToolParam:
        return ToolParam(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL completa a la que hacer el GET.",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Headers HTTP adicionales (opcional).",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["url"],
            },
        )

    def _check_url(self, url: str) -> str | None:
        """
        Valida la URL y devuelve un mensaje de error si no es permitida,
        o None si es válida.
        """
        try:
            parsed = httpx.URL(url)
        except Exception:
            return f"URL inválida: {url!r}"

        if parsed.scheme not in ("http", "https"):
            return f"Solo se permiten esquemas http/https, no {parsed.scheme!r}"

        host = parsed.host.lower().lstrip("www.")
        if not self._profile.allows_domain(host):
            return (
                f"Dominio '{host}' no permitido por el perfil '{self._profile.name}'. "
                f"Dominios permitidos: {list(self._profile.allowed_domains)}"
            )

        return None

    async def run(self, arguments: dict[str, Any]) -> ActionResult:
        url = arguments.get("url", "")
        extra_headers = arguments.get("headers", {})

        error = self._check_url(url)
        if error:
            return ActionResult.error(error)

        original_host = httpx.URL(url).host.lower()

        def check_redirect(request: httpx.Request, response: httpx.Response) -> None:
            location = response.headers.get("location", "")
            if location:
                try:
                    redirect_host = httpx.URL(location).host.lower()
                    if redirect_host and redirect_host != original_host:
                        raise httpx.TooManyRedirects(
                            f"Redirección a dominio externo bloqueada: {redirect_host}",
                            request=request,
                        )
                except httpx.InvalidURL:
                    pass

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=15.0,
                event_hooks={"response": [check_redirect]},
                headers={"User-Agent": "nixbox/0.1"},
            ) as client:
                response = await client.get(url, headers=extra_headers)
                response.raise_for_status()

                content_type = response.headers.get("content-type", "")
                if "text" in content_type or "json" in content_type:
                    return ActionResult.ok(response.text[:32_000])
                else:
                    return ActionResult.error(
                        f"Tipo de contenido no soportado: {content_type!r}"
                    )

        except httpx.TooManyRedirects as exc:
            return ActionResult.error(str(exc))
        except httpx.TimeoutException:
            return ActionResult.error(f"Timeout al conectar con {url!r}")
        except httpx.HTTPStatusError as exc:
            return ActionResult.error(
                f"HTTP {exc.response.status_code} al acceder a {url!r}"
            )
        except httpx.RequestError as exc:
            return ActionResult.error(f"Error de red: {exc}")


def init(profile: SandboxProfile) -> None:
    register(HttpGetAction(profile))
