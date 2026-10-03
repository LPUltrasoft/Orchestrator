"""Telegram por long polling: el gateway habla directo con la Bot API.

Con webhook, Telegram exige una URL HTTPS pública (túnel, dominio, certificado).
Con long polling es el gateway el que sale a buscar los mensajes: no hace falta
nada de eso y ningún puerto del sistema queda expuesto a internet.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import unquote

import httpx

from . import config

log = logging.getLogger("orchestrator.telegram")

# Telegram rechaza mensajes de más de 4096 caracteres.
MAX_MESSAGE = 4000
_OFFSET_FILE = config.STATE_DIR / "telegram_offset"

# (chat_id, texto o None si no es texto, nombre de pila)
MessageHandler = Callable[[str, str | None, str | None], Awaitable[None]]


class TelegramError(RuntimeError):
    pass


_FILE_LINK = re.compile(r"\[([^\]]+)\]\(file://[^)]*\)")
_WEB_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_FILE_URL = re.compile(r"file://\S+")
_BOLD = re.compile(r"\*\*(.+?)\*\*")  # `__` no: rompería __init__.py
_HEADING = re.compile(r"^#{1,6}\s+", re.M)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def to_plain_text(text: str) -> str:
    """Saca el Markdown que los modelos usan aunque el prompt se lo prohíba.

    Telegram sin parse_mode muestra los `**` crudos, y un link file:// a una ruta
    del vault no abre en el celular: se deja solo el nombre.
    """
    text = _FILE_LINK.sub(r"\1", text)
    text = _WEB_LINK.sub(r"\1 (\2)", text)
    text = _BARE_FILE_URL.sub(lambda m: unquote(m.group().rstrip("/").rsplit("/", 1)[-1]), text)
    text = _BOLD.sub(r"\1", text)
    text = _HEADING.sub("", text)
    return _INLINE_CODE.sub(r"\1", text)


def split_message(text: str) -> list[str]:
    """Parte un texto largo en mensajes, cortando en saltos de línea si se puede."""
    text = text.strip() or "(vacío)"
    chunks = []
    while len(text) > MAX_MESSAGE:
        cut = text.rfind("\n", 0, MAX_MESSAGE)
        if cut < MAX_MESSAGE // 2:
            cut = MAX_MESSAGE
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


def _load_offset() -> int | None:
    try:
        return int(_OFFSET_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _save_offset(offset: int) -> None:
    _OFFSET_FILE.write_text(str(offset))


class TelegramBot:
    def __init__(self, token: str, api_base: str) -> None:
        self._url = f"{api_base.rstrip('/')}/bot{token}"
        # El timeout HTTP tiene que superar al del long polling, o cada espera
        # sin mensajes termina en error.
        self._client = httpx.AsyncClient(timeout=config.TELEGRAM_POLL_TIMEOUT + 15)
        self.username: str | None = None
        self.last_error: str | None = None

    async def call(self, method: str, **params: object) -> object:
        response = await self._client.post(f"{self._url}/{method}", json=params)
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(
                f"{method}: {data.get('error_code')} {data.get('description')}"
            )
        return data["result"]

    async def send(self, chat_id: str, text: str) -> None:
        """Texto plano: el Markdown de Telegram rechaza el mensaje entero si un
        carácter queda desbalanceado, y una respuesta perdida es peor que fea."""
        for chunk in split_message(to_plain_text(text)):
            await self.call("sendMessage", chat_id=chat_id, text=chunk)

    async def typing(self, chat_id: str) -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
        except (httpx.HTTPError, TelegramError, ValueError):
            pass  # cosmético: nunca debe tumbar un trabajo

    async def _handshake(self) -> None:
        me = await self.call("getMe")
        hook = await self.call("getWebhookInfo")
        if hook.get("url"):
            # Webhook y getUpdates son excluyentes: con un webhook activo, cada
            # polling devuelve 409. Pasa si n8n publicó un Telegram Trigger.
            raise TelegramError(
                f"el bot @{me.get('username')} tiene un webhook activo ({hook['url']}). "
                "Despublicá el workflow de n8n o llamá a deleteWebhook."
            )
        self.username = me.get("username")
        log.info("bot @%s escuchando por long polling", self.username)

    async def poll_forever(self, handle: MessageHandler) -> None:
        offset = _load_offset()
        backoff = 1
        while True:
            try:
                if self.username is None:
                    await self._handshake()
                updates = await self.call(
                    "getUpdates",
                    offset=offset,
                    timeout=config.TELEGRAM_POLL_TIMEOUT,
                    allowed_updates=["message"],
                )
            except (httpx.HTTPError, TelegramError, ValueError) as exc:
                # Al bootear la red puede no estar lista: reintentar, nunca morir.
                self.last_error = str(exc)
                log.warning("Telegram falló (%s); reintento en %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            backoff = 1
            self.last_error = None
            for update in updates:
                # Confirmar antes de procesar: un mensaje que hace fallar al
                # handler no tiene que volver en loop después de un reinicio.
                offset = update["update_id"] + 1
                _save_offset(offset)
                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if not chat_id:
                    continue
                try:
                    await handle(
                        chat_id,
                        message.get("text"),
                        (message.get("from") or {}).get("first_name"),
                    )
                except Exception:  # noqa: BLE001 - un mensaje no tumba el bot
                    log.exception("falló el manejo de un mensaje de %s", chat_id)

    async def close(self) -> None:
        await self._client.aclose()
