"""Telegram por long polling: el gateway habla directo con la Bot API.

Con webhook, Telegram exige una URL HTTPS pública (túnel, dominio, certificado).
Con long polling es el gateway el que sale a buscar los mensajes: no hace falta
nada de eso y ningún puerto del sistema queda expuesto a internet.
"""
from __future__ import annotations

import asyncio
import json
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
# (chat_id, nombre de pila, data del botón, message_id, callback_query_id)
CallbackHandler = Callable[[str, str | None, str, int, str], Awaitable[None]]
# Filas de botones: [[("✅ Aprobar", "apr:abc:ok"), ...], ...]
Buttons = list[list[tuple[str, str]]]


def _keyboard(buttons: Buttons | None) -> dict:
    """Sin botones = teclado vacío: así una edición los saca del mensaje."""
    return {"inline_keyboard": [
        [{"text": label, "callback_data": data} for label, data in row]
        for row in buttons or []
    ]}


class TelegramError(RuntimeError):
    pass


_FILE_LINK = re.compile(r"\[([^\]]+)\]\(file://[^)]*\)")
_WEB_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BARE_FILE_URL = re.compile(r"file://\S+")
_BOLD = re.compile(r"\*\*(.+?)\*\*")  # `__` no: rompería __init__.py
_HEADING = re.compile(r"^#{1,6}\s+", re.M)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
# Marcadores internos de agy que a veces se cuelan en la respuesta, por ejemplo
# <WAITING_FOR_EVENTS>No message content provided.</WAITING_FOR_EVENTS>.
_INTERNAL_BLOCK = re.compile(r"<([A-Z][A-Z0-9_]{2,})>.*?</\1>\s*", re.S)
_INTERNAL_TAG = re.compile(r"</?[A-Z][A-Z0-9_]{2,}>\s*")


# Avisos internos de agy que a veces se cuelan al principio de la respuesta, por
# ejemplo cuando una herramienta falla y agy reintenta solo:
# "An error occurred while calling the tool. Execution will continue with the original
# message. WaitMsBeforeAsync is too small. Please set a larger value and retry."
_AGY_TOOL_ERROR = re.compile(
    r"An error occurred while calling the tool\.\s*Execution will continue with the original message\."
    r"(?:[^\n]*?Please set a larger value and retry\.)?\s*"
)


def to_plain_text(text: str) -> str:
    """Saca el Markdown que los modelos usan aunque el prompt se lo prohíba.

    Telegram sin parse_mode muestra los `**` crudos, y un link file:// a una ruta
    del vault no abre en el celular: se deja solo el nombre.
    """
    text = _INTERNAL_BLOCK.sub("", text)
    text = _INTERNAL_TAG.sub("", text)
    text = _AGY_TOOL_ERROR.sub("", text)
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

    async def send_buttons(self, chat_id: str, text: str, buttons: Buttons) -> int:
        """Un mensaje con botones. Devuelve su message_id para editarlo después."""
        sent = await self.call(
            "sendMessage", chat_id=chat_id, text=to_plain_text(text)[:MAX_MESSAGE],
            reply_markup=_keyboard(buttons),
        )
        return sent["message_id"]

    async def edit(self, chat_id: str, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        try:
            await self.call(
                "editMessageText", chat_id=chat_id, message_id=message_id,
                text=to_plain_text(text)[:MAX_MESSAGE], reply_markup=_keyboard(buttons),
            )
        except TelegramError as exc:
            if "not modified" not in str(exc):
                raise

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Telegram muestra un relojito en el botón hasta que se responde el toque."""
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except (httpx.HTTPError, TelegramError, ValueError):
            pass

    async def send_album(self, chat_id: str, photos: list[tuple[bytes, str]]) -> None:
        """Capturas como álbum (de a 10, el máximo de Telegram), cada una con su texto."""
        for start in range(0, len(photos), 10):
            chunk = photos[start:start + 10]
            if len(chunk) == 1:
                image, caption = chunk[0]
                files = {"photo": _photo_file("imagen", image)}
                data = {"chat_id": chat_id, "caption": caption[:1024]}
                method = "sendPhoto"
            else:
                files = {
                    f"p{i}": _photo_file(f"imagen{i}", image)
                    for i, (image, _) in enumerate(chunk)
                }
                media = [
                    {"type": "photo", "media": f"attach://p{i}", "caption": caption[:1024]}
                    for i, (_, caption) in enumerate(chunk)
                ]
                data = {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)}
                method = "sendMediaGroup"
            response = await self._client.post(f"{self._url}/{method}", data=data, files=files)
            result = response.json()
            if not result.get("ok"):
                raise TelegramError(f"{method}: {result.get('error_code')} {result.get('description')}")

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

    async def poll_forever(
        self, handle: MessageHandler, on_callback: CallbackHandler | None = None
    ) -> None:
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
                    allowed_updates=["message", "callback_query"],
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
                callback = update.get("callback_query")
                if callback and on_callback:
                    try:
                        await on_callback(
                            str(((callback.get("message") or {}).get("chat") or {}).get("id", "")),
                            (callback.get("from") or {}).get("first_name"),
                            callback.get("data") or "",
                            (callback.get("message") or {}).get("message_id", 0),
                            callback.get("id", ""),
                        )
                    except Exception:  # noqa: BLE001 - un toque no tumba el bot
                        log.exception("falló el manejo de un botón")
                    continue
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


def _photo_file(name: str, image: bytes) -> tuple[str, bytes, str]:
    """Nombre y tipo según el contenido: las capturas de Stitch son PNG; las de agy, JPEG."""
    if image[:3] == b"\xff\xd8\xff":
        return f"{name}.jpg", image, "image/jpeg"
    return f"{name}.png", image, "image/png"

