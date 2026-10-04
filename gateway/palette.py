"""Paleta de colores del proyecto: la única fuente de colores de todo el producto.

La propone el rol UI en `Diseño/DESIGN.md`, en una sección «Paleta» con un bloque JSON;
el usuario la aprueba por Telegram viendo una imagen con los colores; y después la
respetan todos: el gateway se la exige a Stitch en cada pantalla, Imágenes y Frontend la
usan, y el Revisor UX/UI controla que nadie se salga.

    [{"rol": "primario", "nombre": "Verde cancha", "hex": "#1B7F37",
      "uso": "Botones principales, links y elementos activos"}, ...]
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import vault

DESIGN_MD = "Diseño/DESIGN.md"
REQUIRED_ROLES = ("primario", "sobre-primario", "fondo", "superficie", "texto", "error", "exito")
_SECTION = re.compile(r"^#{2,3}\s.*paleta.*$", re.I | re.M)
_JSON_BLOCK = re.compile(r"```json\s*(\[.*?\])\s*```", re.S)
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
# (texto, fondo, mínimo): las combinaciones que la interfaz usa sí o sí. 4,5 es el mínimo
# de WCAG AA para texto normal; 3 para elementos que no son texto.
_PAIRS = (
    ("texto", "fondo", 4.5),
    ("texto", "superficie", 4.5),
    ("texto-secundario", "fondo", 4.5),
    ("sobre-primario", "primario", 4.5),
    ("sobre-secundario", "secundario", 4.5),
    ("error", "fondo", 4.5),
    ("borde", "fondo", 3.0),
)
_FONTS = ("/usr/share/fonts/noto/NotoSans-{}.ttf", "/usr/share/fonts/TTF/DejaVuSans{}.ttf")


class PaletteError(ValueError):
    pass


def load(project: str) -> list[dict] | None:
    """La paleta del proyecto, o None si UI todavía no la escribió."""
    path = vault.project_path(project) / DESIGN_MD
    if not path.exists():
        return None
    return parse(path.read_text(encoding="utf-8"))


def parse(design_md: str) -> list[dict] | None:
    section = _SECTION.search(design_md)
    if not section:
        return None
    block = _JSON_BLOCK.search(design_md, section.end())
    if not block:
        raise PaletteError("la sección «Paleta» del DESIGN.md no tiene el bloque ```json")
    try:
        colors = json.loads(block.group(1))
    except json.JSONDecodeError as exc:
        raise PaletteError(f"el JSON de la paleta no es válido: {exc}") from exc
    if not isinstance(colors, list) or not colors:
        raise PaletteError("la paleta tiene que ser una lista de colores")
    for color in colors:
        if not isinstance(color, dict) or not _HEX.match(str(color.get("hex", ""))):
            raise PaletteError(f"color sin hex válido (#RRGGBB): {color}")
        color["rol"] = str(color.get("rol", "")).strip().lower()
        color["hex"] = color["hex"].upper()
    return colors


def fingerprint(project: str) -> str:
    """Huella de los colores (rol y hex): cambia si UI toca la paleta después de aprobada."""
    colors = load(project) or []
    canonical = json.dumps(sorted((c["rol"], c["hex"]) for c in colors))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16] if colors else ""


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def checks(colors: list[dict]) -> tuple[list[tuple[str, float, bool]], list[str]]:
    """Contrastes de las combinaciones clave y roles que faltan."""
    by_role = {c["rol"]: c["hex"] for c in colors}
    results = []
    for fg, bg, minimum in _PAIRS:
        if fg in by_role and bg in by_role:
            ratio = contrast(by_role[fg], by_role[bg])
            results.append((f"{fg} sobre {bg}", ratio, ratio >= minimum))
    missing = [r for r in REQUIRED_ROLES if r not in by_role]
    return results, missing


def stitch_hint(colors: list[dict]) -> str:
    """Lo que se le agrega a cada prompt de Stitch para que no invente colores."""
    listing = "; ".join(f"{c.get('nombre') or c['rol']} {c['hex']} ({c.get('uso', c['rol'])})" for c in colors)
    return (
        f"Usá únicamente estos colores de la paleta del proyecto, sin agregar otros ni "
        f"variantes: {listing}."
    )


def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for pattern in _FONTS:
        path = Path(pattern.format("-Bold" if bold else ("-Regular" if "noto" in pattern else "")))
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def swatch(colors: list[dict], title: str) -> bytes:
    """Imagen PNG de la paleta: un bloque por color, con su nombre, uso y contrastes."""
    width, row, block, pad = 1080, 132, 300, 36
    results, missing = checks(colors)
    footer = len(results) + (1 if missing else 0)
    height = 120 + row * len(colors) + 70 + 46 * footer + pad
    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    draw.text((pad, 40), title, fill="#1A1A1A", font=_font(True, 40))

    y = 120
    for color in colors:
        hex_color = color["hex"]
        on_color = "#FFFFFF" if contrast("#FFFFFF", hex_color) >= contrast("#1A1A1A", hex_color) else "#1A1A1A"
        draw.rounded_rectangle((pad, y, pad + block, y + row - 16), radius=16, fill=hex_color,
                               outline="#D0D0D0" if _luminance(hex_color) > 0.85 else None, width=2)
        draw.text((pad + 20, y + row // 2 - 26), hex_color, fill=on_color, font=_font(True, 30))
        x = pad + block + 30
        name = color.get("nombre") or color["rol"]
        draw.text((x, y + 6), f"{name}  ·  {color['rol']}", fill="#1A1A1A", font=_font(True, 28))
        for i, line in enumerate(textwrap.wrap(str(color.get("uso", "")), 48)[:2]):
            draw.text((x, y + 48 + i * 32), line, fill="#555555", font=_font(False, 24))
        y += row

    y += 20
    draw.text((pad, y), "Contraste (WCAG AA)", fill="#1A1A1A", font=_font(True, 28))
    y += 50
    for label, ratio, ok in results:
        text = f"{'OK ' if ok else 'NO CUMPLE '} {label}: {ratio:.1f}:1".replace(".", ",")
        draw.text((pad, y), text, fill="#1B7F37" if ok else "#B3261E", font=_font(False, 24))
        y += 46
    if missing:
        draw.text((pad, y), f"Faltan roles: {', '.join(missing)}", fill="#B3261E", font=_font(False, 24))

    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def summary(colors: list[dict]) -> str:
    """Texto corto para el pedido de aprobación: los contrastes que no cumplen."""
    results, missing = checks(colors)
    failing = [f"{label} ({ratio:.1f}:1)".replace(".", ",") for label, ratio, ok in results if not ok]
    notes = []
    if failing:
        notes.append("⚠️ No cumplen el contraste AA: " + "; ".join(failing) + ".")
    if missing:
        notes.append("⚠️ Faltan roles: " + ", ".join(missing) + ".")
    return " ".join(notes) or "✅ Todos los contrastes clave cumplen AA."
