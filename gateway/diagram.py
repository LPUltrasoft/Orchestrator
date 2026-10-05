"""Diagramas para Telegram: el DER que escribe el DBA en el 03, en PNG.

Se dibuja con la herramienta oficial de Mermaid (mmdc) y el Chromium de la PC, así sale
igual que en Obsidian. Todo local: el esquema de la base de un cliente no sale de la PC.
Instalación: scripts/instalar-mermaid.sh.
"""
from __future__ import annotations

import re
import struct
import subprocess
import tempfile
from pathlib import Path

from . import config

_MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(.*?)```", re.S)
DATA_MODEL_DOC = "03 - Modelo de Datos"


class DiagramError(RuntimeError):
    pass


def _node() -> Path | None:
    versions = sorted(Path.home().glob(".nvm/versions/node/*/bin/node"), key=lambda p: [
        int(x) if x.isdigit() else 0 for x in p.parent.parent.name.lstrip("v").split(".")])
    return versions[-1] if versions else None


def available() -> bool:
    return bool(_node()) and (config.MERMAID_DIR / "node_modules/.bin/mmdc").exists()


def er_blocks(text: str) -> list[str]:
    """Los diagramas entidad-relación (```mermaid erDiagram) de un documento."""
    return [b.strip() for b in _MERMAID_BLOCK.findall(text) if b.lstrip().startswith("erDiagram")]


def render(source: str, theme: str = "neutral") -> bytes:
    """PNG del diagrama, a escala 2 y con fondo blanco. Lanza DiagramError si no puede."""
    node = _node()
    if not node or not available():
        raise DiagramError("falta la herramienta de Mermaid: correr scripts/instalar-mermaid.sh")
    with tempfile.TemporaryDirectory(prefix="der-") as tmp:
        src, out = Path(tmp) / "der.mmd", Path(tmp) / "der.png"
        src.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [str(node), str(config.MERMAID_DIR / "node_modules/.bin/mmdc"), "-i", str(src), "-o", str(out),
             "-p", str(config.MERMAID_DIR / "puppeteer.json"), "-b", "white", "-t", theme, "-s", "2", "-q"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0 or not out.exists():
            raise DiagramError(f"Mermaid no pudo dibujar el diagrama: {(result.stderr or result.stdout).strip()[-300:]}")
        return out.read_bytes()


def size(png: bytes) -> tuple[int, int]:
    """Ancho y alto de un PNG (del encabezado IHDR)."""
    return struct.unpack(">II", png[16:24])
