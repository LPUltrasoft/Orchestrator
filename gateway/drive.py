"""Espacio del Google Drive de los backups, para avisar antes de que se llene.

Los 15 GB gratis de Google se comparten entre Drive, Gmail y Fotos: lo que importa es
el uso total de la cuenta. `rclone about` lo informa aunque el permiso sea `drive.file`
(rclone solo ve sus archivos, pero la cuota es de la cuenta).
"""
from __future__ import annotations

import asyncio
import json
import logging

from . import config

log = logging.getLogger("orchestrator.drive")

GIB = 1024 ** 3
BACKUPS_FOLDER = "Backups Orchestrator"


async def _rclone(*args: str) -> tuple[dict, str]:
    process = await asyncio.create_subprocess_exec(
        config.RCLONE_BIN, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), timeout=120)
    if process.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace").strip()[-300:] or f"rclone {args[0]} falló")
    return json.loads(out), err.decode("utf-8", "replace")


async def usage() -> dict:
    """Uso de la cuenta en bytes: total, usado, y de qué. Lanza RuntimeError si no puede."""
    remote = f"{config.DRIVE_REMOTE}:"
    about, err = await _rclone("about", remote, "--json")
    try:
        size, _ = await _rclone("size", f"{remote}{BACKUPS_FOLDER}", "--json")
        backups = size.get("bytes", 0)
    except RuntimeError:
        backups = 0  # la carpeta todavía no existe
    total = about.get("total", 0)
    used = total - about.get("free", 0)
    drive = about.get("used", 0)
    trashed = about.get("trashed", 0)
    return {
        "total": total,
        "usado": used,
        "backups": backups,
        "papelera": trashed,
        "otros_drive": max(drive - trashed - backups, 0),
        "gmail_y_fotos": about.get("other", 0),
        # rclone avisa en cada llamada si usa su client_id compartido, que deja de andar.
        "client_id_compartido": "shared Google Drive client_id" in err,
    }


def gb(value: int) -> str:
    return f"{value / GIB:.1f} GB".replace(".", ",")


def alert_text(u: dict) -> str:
    return (
        f"💾 El Google Drive de los backups llegó a {gb(u['usado'])} de {gb(u['total'])}.\n\n"
        f"- Backups: {gb(u['backups'])}\n"
        f"- Papelera de Drive: {gb(u['papelera'])}\n"
        f"- Otros archivos de Drive: {gb(u['otros_drive'])}\n"
        f"- Gmail y Fotos: {gb(u['gmail_y_fotos'])}\n\n"
        "Para liberar espacio: vaciá la papelera de Drive, borrá backups viejos de la carpeta "
        f"«{BACKUPS_FOLDER}» o revisá Gmail y Fotos."
    )
