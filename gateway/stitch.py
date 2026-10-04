"""Cliente del MCP oficial de Stitch (Google Labs): diseño de pantallas con IA.

El servidor (https://stitch.googleapis.com/mcp) es sin estado y responde JSON plano a
JSON-RPC, así que alcanza con un POST por llamada. Se autentica con una API key que se
crea en Stitch → Settings → API Keys, en el header X-Goog-Api-Key.

Lo llama el gateway y no un agente: así el gateway puede bajar las capturas, guardarlas
en el vault y mandarlas por Telegram, y la API key queda en el .env.
"""
from __future__ import annotations

import base64
import itertools
import json
import logging

import httpx

from . import config

log = logging.getLogger("orchestrator.stitch")

_ids = itertools.count(1)


class StitchError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.STITCH_API_KEY)


def _short(resource: str) -> str:
    """'projects/123/screens/abc' -> 'abc'. Stitch pide los ids sin prefijo."""
    return resource.rstrip("/").rsplit("/", 1)[-1]


def _payload(result: dict) -> object:
    """El resultado de una herramienta: structuredContent, o el JSON del texto."""
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content", [])]
        raise StitchError(" ".join(texts) or "Stitch devolvió un error")
    if "structuredContent" in result:
        return result["structuredContent"]
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except (json.JSONDecodeError, KeyError):
                return item.get("text")
    return result


def find_resources(data: object, kind: str) -> list[dict]:
    """Objetos con un 'name' del tipo projects/…/<kind>/…, en cualquier profundidad.

    No dependemos de la forma exacta de cada respuesta: buscamos los recursos."""
    found = []
    if isinstance(data, dict):
        name = data.get("name")
        if isinstance(name, str) and f"/{kind}/" in name:
            found.append(data)
        for value in data.values():
            found += find_resources(value, kind)
    elif isinstance(data, list):
        for value in data:
            found += find_resources(value, kind)
    return found


class Stitch:
    def __init__(self, api_key: str | None = None, url: str | None = None) -> None:
        self._key = api_key or config.STITCH_API_KEY
        self._url = url or config.STITCH_URL
        if not self._key:
            raise StitchError("falta STITCH_API_KEY en el .env (se crea en Stitch → Settings → API Keys)")
        # Generar una pantalla puede tardar más de un minuto.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20))

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, tool: str, **arguments: object) -> object:
        request = {
            "jsonrpc": "2.0",
            "id": next(_ids),
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        response = await self._client.post(
            self._url,
            json=request,
            headers={
                "X-Goog-Api-Key": self._key,
                "Accept": "application/json, text/event-stream",
            },
        )
        if response.status_code >= 400:
            raise StitchError(f"{tool}: HTTP {response.status_code} {response.text[:300]}")
        data = response.json()
        if "error" in data:
            raise StitchError(f"{tool}: {data['error'].get('message', data['error'])}")
        return _payload(data.get("result") or {})

    # ── proyectos y sistema de diseño ───────────────────────────────────────────

    async def create_project(self, title: str) -> str:
        data = await self.call("create_project", title=title)
        projects = find_resources(data, "projects") or ([data] if isinstance(data, dict) else [])
        name = next((p["name"] for p in projects if str(p.get("name", "")).startswith("projects/")), None)
        if not name and isinstance(data, dict) and str(data.get("name", "")).startswith("projects/"):
            name = data["name"]
        if not name:
            raise StitchError(f"create_project no devolvió el id del proyecto: {str(data)[:200]}")
        return _short(name)

    async def design_system_from_md(self, project_id: str, design_md: str) -> str | None:
        """Sube el DESIGN.md y lo convierte en sistema de diseño. Devuelve su asset."""
        instance = await self.call(
            "upload_design_md",
            projectId=project_id,
            designMdBase64=base64.b64encode(design_md.encode("utf-8")).decode("ascii"),
        )
        if not isinstance(instance, dict) or not instance.get("id"):
            raise StitchError(f"upload_design_md no devolvió la instancia: {str(instance)[:200]}")
        await self.call(
            "create_design_system_from_design_md",
            projectId=project_id,
            deviceType="MOBILE",
            selectedScreenInstance={"id": instance["id"], "sourceScreen": instance.get("sourceScreen", "")},
        )
        systems = await self.call("list_design_systems", projectId=project_id)
        assets = [
            d["name"] for d in find_resources(systems, "assets")
        ] or [
            v for v in _walk_strings(systems) if v.startswith("assets/")
        ]
        return assets[-1] if assets else None

    # ── pantallas ───────────────────────────────────────────────────────────────

    async def screen_names(self, project_id: str) -> list[str]:
        data = await self.call("list_screens", projectId=project_id)
        return [s["name"] for s in find_resources(data, "screens")]

    async def generate(
        self, project_id: str, prompt: str, design_system: str | None, device: str = "MOBILE"
    ) -> str:
        """Genera una pantalla para ese dispositivo y devuelve su nombre de recurso."""
        before = set(await self.screen_names(project_id))
        arguments: dict[str, object] = {
            "projectId": project_id,
            "prompt": prompt,
            "deviceType": device,
            "modelId": config.STITCH_MODEL,
        }
        if design_system:
            arguments["designSystem"] = design_system
        data = await self.call("generate_screen_from_text", **arguments)
        return await self._new_screen(project_id, before, data)

    async def edit(self, project_id: str, screen: str, change: str, device: str = "MOBILE") -> str:
        """Aplica un cambio a una pantalla. Devuelve la pantalla resultante."""
        before = set(await self.screen_names(project_id))
        data = await self.call(
            "edit_screens",
            projectId=project_id,
            selectedScreenIds=[_short(screen)],
            prompt=change,
            deviceType=device,
            modelId=config.STITCH_MODEL,
        )
        try:
            return await self._new_screen(project_id, before, data)
        except StitchError:
            return screen  # la editó en el lugar

    async def _new_screen(self, project_id: str, before: set[str], data: object) -> str:
        created = [s["name"] for s in find_resources(data, "screens") if s["name"] not in before]
        if not created:
            created = [n for n in await self.screen_names(project_id) if n not in before]
        if not created:
            raise StitchError("Stitch no creó ninguna pantalla nueva")
        return created[-1]

    async def files(self, screen: str) -> tuple[bytes, str]:
        """La captura (PNG) y el HTML de una pantalla."""
        data = await self.call("get_screen", name=screen)
        if not isinstance(data, dict):
            raise StitchError(f"get_screen devolvió algo inesperado: {str(data)[:200]}")
        shot = (data.get("screenshot") or {}).get("downloadUrl")
        html = (data.get("htmlCode") or {}).get("downloadUrl")
        if not shot:
            raise StitchError("la pantalla no tiene captura todavía")
        image = await self._download(shot, image=True)
        code = (await self._download(html)).decode("utf-8", "replace") if html else ""
        return image, code

    async def _download(self, url: str, image: bool = False) -> bytes:
        # Las imágenes son URLs base de FIFE: con "=s0" se pide el tamaño original.
        candidates = [url, f"{url}=s0"] if image and "=" not in url.rsplit("/", 1)[-1] else [url]
        last = ""
        for candidate in candidates:
            for headers in ({}, {"X-Goog-Api-Key": self._key}):
                response = await self._client.get(candidate, headers=headers, follow_redirects=True)
                if response.status_code < 400 and (not image or response.content[:8] == b"\x89PNG\r\n\x1a\n"
                                                   or response.headers.get("content-type", "").startswith("image/")):
                    return response.content
                last = f"HTTP {response.status_code} {response.headers.get('content-type')}"
        raise StitchError(f"no pude descargar {url[:80]}…: {last}")


def _walk_strings(data: object):
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for value in data.values():
            yield from _walk_strings(value)
    elif isinstance(data, list):
        for value in data:
            yield from _walk_strings(value)
