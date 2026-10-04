"""Pantallas de un proyecto generadas con Stitch.

Los proyectos son webs mobile first que tienen que funcionar en cualquier resolución:
por eso cada pantalla se genera, por defecto, para celular y para escritorio (y para
tablet, si se pide).

El rol UI escribe en la carpeta «Diseño» del proyecto:
- `DESIGN.md`: el sistema de diseño (colores, tipografía, componentes, breakpoints).
- `Pantallas.md`: la lista de pantallas, en un bloque JSON con id, título, prompt y,
  opcionalmente, dispositivos.

El gateway genera cada pantalla en Stitch con ese sistema de diseño, baja la captura y
el HTML de cada dispositivo a «Diseño/Pantallas/», arma un índice para verlas en
Obsidian y anota en «Diseño/stitch.json» qué pantalla de Stitch corresponde a cada una.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import stitch, vault

log = logging.getLogger("orchestrator.design")

DESIGN_DIR = "Diseño"
SCREENS_FILE = "Pantallas.md"
DESIGN_MD = "DESIGN.md"
STATE_FILE = "stitch.json"
INDEX_FILE = "Pantallas generadas.md"
_JSON_BLOCK = re.compile(r"```json\s*(\[.*?\])\s*```", re.S)

# Celular y escritorio por defecto: los extremos. Tablet queda cubierta por las reglas
# responsive del DESIGN.md y se genera solo si se pide (cada versión es una generación).
DEFAULT_DEVICES = ("MOBILE", "DESKTOP")
DEVICE_LABEL = {"MOBILE": "celular", "TABLET": "tablet", "DESKTOP": "escritorio"}
_DEVICE_ALIASES = {
    "mobile": "MOBILE", "movil": "MOBILE", "móvil": "MOBILE", "celular": "MOBILE",
    "tablet": "TABLET",
    "desktop": "DESKTOP", "escritorio": "DESKTOP", "web": "DESKTOP",
}


@dataclass(frozen=True)
class ScreenSpec:
    id: str
    title: str
    prompt: str
    devices: tuple[str, ...] = DEFAULT_DEVICES


@dataclass(frozen=True)
class Rendered:
    id: str
    title: str
    device: str
    png: Path
    html: Path

    @property
    def label(self) -> str:
        return f"{self.title} · {DEVICE_LABEL[self.device]}"


# (qué se está generando, "start" | "done" | "error", detalle)
Callback = Callable[[str, str, str], None]


def folder(project: str) -> Path:
    return vault.project_path(project) / DESIGN_DIR


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _devices(raw: object) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_DEVICES
    devices = []
    for value in raw if isinstance(raw, list) else [raw]:
        device = _DEVICE_ALIASES.get(str(value).strip().lower())
        if not device:
            raise ValueError(f"Dispositivo desconocido: «{value}». Válidos: celular, tablet, escritorio.")
        if device not in devices:
            devices.append(device)
    # Siempre en el mismo orden: del más chico al más grande.
    return tuple(d for d in ("MOBILE", "TABLET", "DESKTOP") if d in devices)


def specs(project: str) -> list[ScreenSpec]:
    """Las pantallas que definió UI. Error claro si el archivo falta o está mal."""
    path = folder(project) / SCREENS_FILE
    if not path.exists():
        raise ValueError(
            f"No existe «{DESIGN_DIR}/{SCREENS_FILE}» en «{project}»: primero tiene que "
            "escribirlo el rol ui, con la lista de pantallas para Stitch."
        )
    match = _JSON_BLOCK.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"«{DESIGN_DIR}/{SCREENS_FILE}» no tiene el bloque ```json con las pantallas.")
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"El JSON de «{DESIGN_DIR}/{SCREENS_FILE}» no es válido: {exc}") from exc
    screens, seen = [], set()
    for item in raw:
        screen_id = re.sub(r"[^a-z0-9-]+", "-", str(item.get("id", "")).lower()).strip("-")
        prompt = str(item.get("prompt", "")).strip()
        if not screen_id or not prompt:
            raise ValueError(f"Cada pantalla necesita «id» y «prompt»: {item}")
        if screen_id in seen:
            raise ValueError(f"Pantalla repetida: «{screen_id}»")
        seen.add(screen_id)
        screens.append(ScreenSpec(
            screen_id, str(item.get("titulo") or screen_id), prompt, _devices(item.get("dispositivos"))
        ))
    if not screens:
        raise ValueError(f"«{DESIGN_DIR}/{SCREENS_FILE}» no define ninguna pantalla.")
    return screens


def load_state(project: str) -> dict:
    path = folder(project) / STATE_FILE
    if not path.exists():
        return {"project_id": None, "design_system": None, "design_md_hash": None, "screens": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(project: str, state: dict) -> Path:
    path = folder(project) / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _file_stem(spec_id: str, device: str) -> str:
    return f"{spec_id}-{DEVICE_LABEL[device]}"


def _save_files(project: str, spec_id: str, device: str, png: bytes, html: str) -> tuple[Path, Path]:
    target = folder(project) / "Pantallas"
    target.mkdir(parents=True, exist_ok=True)
    stem = _file_stem(spec_id, device)
    png_path, html_path = target / f"{stem}.png", target / f"{stem}.html"
    png_path.write_bytes(png)
    html_path.write_text(html, encoding="utf-8")
    return png_path, html_path


def _write_index(project: str, state: dict) -> Path:
    blocks = []
    for spec_id, entry in state["screens"].items():
        devices = [d for d in ("MOBILE", "TABLET", "DESKTOP") if d in entry.get("dispositivos", {})]
        # Celular angosto, escritorio ancho: cada captura con un ancho acorde.
        widths = {"MOBILE": 260, "TABLET": 380, "DESKTOP": 560}
        images = "\n\n".join(
            f"**{DEVICE_LABEL[d].capitalize()}**\n\n"
            f"![[{DESIGN_DIR}/Pantallas/{_file_stem(spec_id, d)}.png|{widths[d]}]]"
            for d in devices
        )
        changes = "\n".join(f"- {c['fecha'][:10]}: {c['cambio']}" for c in entry.get("cambios", []))
        blocks.append(
            f"## {entry['titulo']}\n\n{images}\n\n"
            f"- Generada: {entry['fecha'][:16].replace('T', ' ')} UTC\n"
            + (f"\n**Cambios pedidos**\n{changes}\n" if changes else "")
        )
    path = folder(project) / INDEX_FILE
    path.write_text(
        "# 🖼️ Pantallas generadas\n\n"
        "> Generadas con Stitch a partir de «Pantallas.md» y «DESIGN.md», para celular y "
        "escritorio. Las mantiene el sistema: no las edites a mano.\n\n---\n\n"
        + "\n---\n\n".join(blocks),
        encoding="utf-8",
    )
    return path


def _commit(project: str, paths: list[Path], message: str) -> None:
    vault.commit_paths([vault.relative(p) for p in paths], f"diseño: {project}: {message}")


async def generate(
    project: str, only: list[str] | None = None, on_screen: Callback | None = None
) -> tuple[list[Rendered], list[str]]:
    """Genera las pantallas en Stitch, en cada dispositivo. Devuelve las logradas y los
    errores: si una falla, sigue con las demás."""
    screens = specs(project)
    if only:
        unknown = set(only) - {s.id for s in screens}
        if unknown:
            raise ValueError(f"No están en «{SCREENS_FILE}»: {', '.join(sorted(unknown))}")
        screens = [s for s in screens if s.id in only]
    notify = on_screen or (lambda *_: None)
    state = load_state(project)
    client = stitch.Stitch()
    rendered: list[Rendered] = []
    errors: list[str] = []
    touched: list[Path] = []
    try:
        if not state["project_id"]:
            state["project_id"] = await client.create_project(project)
            touched.append(_save_state(project, state))

        design_md_path = folder(project) / DESIGN_MD
        if design_md_path.exists():
            design_md = design_md_path.read_text(encoding="utf-8")
            if state.get("design_md_hash") != _hash(design_md):
                try:
                    state["design_system"] = await client.design_system_from_md(state["project_id"], design_md)
                    state["design_md_hash"] = _hash(design_md)
                except stitch.StitchError as exc:
                    # Sin sistema de diseño Stitch usa uno por defecto: mejor eso que nada.
                    log.warning("no pude crear el sistema de diseño en Stitch: %s", exc)
                    errors.append(f"sistema de diseño: {exc}")
                touched.append(_save_state(project, state))

        for spec in screens:
            entry = {
                "titulo": spec.title,
                "prompt_hash": _hash(spec.prompt),
                "fecha": _now(),
                "cambios": [],
                "dispositivos": {},
            }
            for device in spec.devices:
                label = f"{spec.title} · {DEVICE_LABEL[device]}"
                notify(label, "start", "")
                try:
                    screen = await client.generate(
                        state["project_id"], spec.prompt, state.get("design_system"), device
                    )
                    png, html = await client.files(screen)
                except stitch.StitchError as exc:
                    log.warning("Stitch falló con %s: %s", label, exc)
                    errors.append(f"{label}: {exc}")
                    notify(label, "error", str(exc))
                    continue
                png_path, html_path = _save_files(project, spec.id, device, png, html)
                entry["dispositivos"][device] = {"screen": screen}
                touched += [png_path, html_path]
                rendered.append(Rendered(spec.id, spec.title, device, png_path, html_path))
                notify(label, "done", "")
            if entry["dispositivos"]:
                state["screens"][spec.id] = entry
                touched.append(_save_state(project, state))
    finally:
        await client.close()
        if touched:
            touched.append(_write_index(project, state))
            _commit(project, sorted(set(touched)), f"{len(rendered)} captura(s) generadas en Stitch")
    return rendered, errors


async def edit(
    project: str, spec_id: str, change: str, on_screen: Callback | None = None
) -> list[Rendered]:
    """Aplica un cambio a una pantalla ya generada, en todos sus dispositivos."""
    state = load_state(project)
    entry = state["screens"].get(spec_id)
    if not entry:
        generated = ", ".join(state["screens"]) or "ninguna"
        raise ValueError(f"La pantalla «{spec_id}» no está generada. Generadas: {generated}.")
    notify = on_screen or (lambda *_: None)
    client = stitch.Stitch()
    rendered: list[Rendered] = []
    touched: list[Path] = []
    try:
        for device, version in entry["dispositivos"].items():
            label = f"{entry['titulo']} · {DEVICE_LABEL[device]}"
            notify(label, "start", "")
            try:
                screen = await client.edit(state["project_id"], version["screen"], change, device)
                png, html = await client.files(screen)
            except stitch.StitchError as exc:
                notify(label, "error", str(exc))
                if not rendered:
                    raise
                continue
            png_path, html_path = _save_files(project, spec_id, device, png, html)
            version["screen"] = screen
            touched += [png_path, html_path]
            rendered.append(Rendered(spec_id, entry["titulo"], device, png_path, html_path))
            notify(label, "done", "")
    finally:
        await client.close()
    entry["fecha"] = _now()
    entry.setdefault("cambios", []).append({"fecha": _now(), "cambio": change})
    touched += [_save_state(project, state), _write_index(project, state)]
    _commit(project, touched, f"cambio en «{entry['titulo']}»")
    return rendered
