"""Proceso de desarrollo de productos: fases, puertas de aprobación y estado.

El orquestador es un LLM y puede saltearse una regla; el gateway no. Por eso las
puertas se imponen acá: no se puede delegar en un rol de una fase futura hasta que el
usuario apruebe la puerta de la fase actual.

El estado de cada proyecto vive en su carpeta del vault, en «Estado del Proyecto.md»:
legible para el usuario, con los datos en un bloque JSON al final y respaldado en git
junto con el resto del vault. Los proyectos anteriores al proceso no tienen ese archivo
y siguen funcionando como antes, sin fases.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, vault

STATE_FILE = "Estado del Proyecto.md"
ADR_DIR = "ADRs"
MESA_DIR = "Mesa Técnica"
_DATA_MARK = "## 🔒 Datos del sistema (no editar)"
_ADR_FILE = re.compile(r"^(ADR-\d{3})\s*-\s*(.+)\.md$")
_ADR_STATUS = re.compile(r"\*\*Estado:\*\*\s*(.+)")


@dataclass(frozen=True)
class Phase:
    number: int
    name: str
    emoji: str
    roles: tuple[str, ...]
    gate: str | None  # puerta que cierra la fase
    available: bool  # implementada en esta etapa del sistema
    guide: str  # qué hacer en esta fase: se lo dice el gateway al orquestador

    @property
    def label(self) -> str:
        return f"{self.number} · {self.emoji} {self.name}"


_NOT_YET = "Esta fase es de la etapa 3 del sistema y todavía no está implementada: avisale al usuario."

PHASES = (
    Phase(1, "Descubrimiento", "🔍", ("producto", "qa"), "alcance", True,
          "Delegá en producto la visión (00) y los requerimientos con el MVP y el backlog "
          "priorizado (01). Después en qa, que revise que cada requisito sea verificable y "
          "escriba los criterios de aceptación en el plan de pruebas (11). Al final pedí la "
          "aprobación «alcance»."),
    Phase(2, "Mesa técnica", "🏛️", ("lider_tecnico", "dba", "producto"), "stack", True,
          "Convocá la mesa técnica con orc-mesa: Líder técnico, DBA y Producto debaten el "
          "stack por rondas y el Líder técnico cierra con ADRs. Cuando el sistema te avise "
          "que terminó, pedí la aprobación de cada ADR (la puerta es su id, por ejemplo "
          "ADR-001) y, con todos aprobados, la aprobación «stack»."),
    Phase(3, "Propuesta y contrato", "⚖️", ("team_leader", "lider_tecnico", "legal"), "contrato", True,
          "Delegá en team_leader las estimaciones de tiempos y costos (08), con la "
          "infraestructura de los dos ambientes por separado; en lider_tecnico la revisión "
          "técnica de esas estimaciones; y en legal la propuesta y el contrato (09). Pedí la "
          "aprobación «contrato»."),
    Phase(4, "Repos y esqueleto", "📦", ("devops",), None, False, _NOT_YET),
    Phase(5, "Diseño y datos", "🎨", ("ux", "ui", "dba", "lider_tecnico", "seguridad"), "diseño", True,
          "Delegá en ux los flujos y wireframes y después en ui el sistema de diseño y las "
          "pantallas (05, mobile first). Además: dba, el modelo y el catálogo de funciones (03), "
          "y lider_tecnico, el contrato de la API en OpenAPI (04) y la tabla de qué cambia "
          "entre desarrollo y producción (02). Opcional: seguridad revisa el diseño (12). "
          "Pedí la aprobación «diseño»."),
    Phase(6, "Planificación", "📌", ("team_leader", "qa"), "plan", True,
          "Delegá en team_leader el plan de trabajo (10) y en qa el plan de pruebas por tarea "
          "(11). Pedí la aprobación «plan»."),
    Phase(7, "Desarrollo", "⚙️", ("nestjs",), None, False, _NOT_YET),
    Phase(8, "Release", "🚦", ("qa", "seguridad", "legal"), "release", False, _NOT_YET),
    Phase(9, "Operación", "📈", ("devops",), None, False, _NOT_YET),
)
_BY_NUMBER = {p.number: p for p in PHASES}
GATES = {p.gate: p.number for p in PHASES if p.gate}
GATE_LABELS = {
    "alcance": "Alcance: MVP y backlog",
    "stack": "Stack técnico",
    "contrato": "Propuesta y contrato",
    "diseño": "Diseño y datos",
    "plan": "Plan de trabajo",
    "release": "Release",
}


# ── utilidades ──────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%d/%m %H:%M")


def normalize_gate(gate: str) -> str:
    gate = gate.strip()
    if re.fullmatch(r"(?i)adr-\d{3}", gate):
        return gate.upper()
    gate = gate.lower()
    return "diseño" if gate == "diseno" else gate


def gate_label(gate: str, project: str | None = None) -> str:
    if gate.startswith("ADR-") and project:
        adr = next((a for a in list_adrs(project) if a["id"] == gate), None)
        return f"{gate} · {adr['title']}" if adr else gate
    return GATE_LABELS.get(gate, gate)


def role_min_phase(role: str) -> int:
    phases = [p.number for p in PHASES if role in p.roles]
    return min(phases) if phases else 1


def phase(number: int) -> Phase:
    return _BY_NUMBER[number]


def _next_phase(after: int) -> int:
    """La próxima fase disponible; si no hay, la siguiente (que explica que falta)."""
    later = [p for p in PHASES if p.number > after]
    available = [p for p in later if p.available]
    if available:
        return available[0].number
    return later[0].number if later else after


# ── estado ──────────────────────────────────────────────────────────────────────


def state_path(project: str) -> Path:
    return vault.project_path(project) / STATE_FILE


def load(project: str) -> dict | None:
    """Estado del proyecto, o None si es anterior al proceso (o no existe)."""
    path = state_path(project)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(re.escape(_DATA_MARK) + r"\s*```json\s*(\{.*?\})\s*```", text, re.S)
    if not match:
        raise RuntimeError(f"«{STATE_FILE}» de {project} no tiene el bloque de datos")
    return json.loads(match.group(1))


def is_new_project(project: str) -> bool:
    path = vault.project_path(project)
    return not path.exists() or not any(p for p in path.iterdir() if not p.name.startswith("."))


def ensure(project: str) -> dict | None:
    """El estado del proyecto. Un proyecto nuevo arranca en la fase 1; uno anterior al
    proceso devuelve None y queda sin fases."""
    state = load(project)
    if state is not None:
        return state
    if not is_new_project(project):
        return None
    state = {
        "version": 1,
        "proyecto": project,
        "fase": 1,
        "creado": _now(),
        "aprobaciones": {},
        "pendientes": {},
        "historial": [],
    }
    save(project, state, "proyecto creado, fase 1")
    return state


def save(project: str, state: dict, event: str) -> str | None:
    state["historial"].append({"fecha": _now(), "evento": event})
    path = state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(project, state), encoding="utf-8")
    return vault.commit_paths([vault.relative(path)], f"estado: {project}: {event}")


def projects_with_state() -> list[tuple[str, dict]]:
    found = []
    for name in vault.list_projects():
        try:
            state = load(name)
        except (RuntimeError, ValueError):
            continue
        if state is not None:
            found.append((name, state))
    return found


# ── puertas ─────────────────────────────────────────────────────────────────────


def check_role(state: dict | None, role: str) -> str | None:
    """Error si el rol todavía no puede trabajar en este proyecto, o None."""
    if state is None:
        return None  # proyecto anterior al proceso: sin fases
    current = phase(state["fase"])
    needed = role_min_phase(role)
    if needed <= state["fase"]:
        return None
    gate = current.gate
    return (
        f"El proyecto está en la fase {current.label}. El rol {role} entra en la fase "
        f"{phase(needed).label}, y para avanzar falta que el usuario apruebe "
        f"«{gate_label(gate) if gate else 'la fase actual'}». Qué hacer ahora: {current.guide}"
    )


def validate_gate(project: str, state: dict | None, gate: str) -> str | None:
    if state is None:
        return f"«{project}» es anterior al proceso: no tiene fases ni puertas."
    if gate.startswith("ADR-"):
        if state["fase"] < 2:
            return "Los ADRs se aprueban a partir de la fase 2 (mesa técnica)."
        adr = next((a for a in list_adrs(project) if a["id"] == gate), None)
        if not adr:
            return f"No existe {gate} en la carpeta «{ADR_DIR}» del proyecto."
        if adr["approved"]:
            return f"{gate} ya está aprobado."
        return None
    if gate not in GATES:
        valid = ", ".join(GATES)
        return f"Puerta desconocida: «{gate}». Válidas: {valid}, o el id de un ADR (ADR-001)."
    if GATES[gate] > state["fase"]:
        return (
            f"«{gate}» es la puerta de la fase {phase(GATES[gate]).label}, y el proyecto "
            f"está en la {phase(state['fase']).label}."
        )
    if gate == "stack":
        pending = [a["id"] for a in list_adrs(project) if not a["approved"]]
        if pending:
            return f"Antes de «stack» falta aprobar: {', '.join(pending)}."
    return None


def request(state: dict, gate: str, summary: str, chat_id: str) -> tuple[str, list[dict]]:
    """Registra un pedido de aprobación. Devuelve su id y los pedidos que reemplaza."""
    replaced = [
        {"id": pid, **entry} for pid, entry in state["pendientes"].items()
        if entry["puerta"] == gate
    ]
    for entry in replaced:
        state["pendientes"].pop(entry["id"])
    approval_id = uuid.uuid4().hex[:10]
    state["pendientes"][approval_id] = {
        "puerta": gate,
        "resumen": summary,
        "chat_id": chat_id,
        "message_id": None,
        "fecha": _now(),
        "esperando_cambios": False,
    }
    return approval_id, replaced


def approve(project: str, state: dict, approval_id: str, by: str) -> tuple[str, int | None]:
    """Aprueba un pedido. Devuelve la puerta y la fase nueva, si avanzó.

    Primero lo que puede fallar (el archivo del ADR) y después el estado: una aprobación
    a medias dejaba el pedido registrado en memoria pero no guardado, con los botones
    vivos y sin despertar al orquestador."""
    gate = state["pendientes"][approval_id]["puerta"]
    if gate.startswith("ADR-"):
        _mark_adr_approved(project, gate, by)
    state["pendientes"].pop(approval_id)
    state["aprobaciones"][gate] = {"fecha": _now(), "por": by}
    advanced = None
    if gate in GATES and GATES[gate] == state["fase"]:
        state["fase"] = advanced = _next_phase(state["fase"])
    return gate, advanced


def find_pending(approval_id: str) -> tuple[str, dict, dict] | None:
    for name, state in projects_with_state():
        if approval_id in state["pendientes"]:
            return name, state, state["pendientes"][approval_id]
    return None


def waiting_changes(chat_id: str) -> tuple[str, dict, str, dict] | None:
    """El pedido para el que este chat está escribiendo cambios, si hay uno."""
    for name, state in projects_with_state():
        for pid, entry in state["pendientes"].items():
            if entry["chat_id"] == chat_id and entry.get("esperando_cambios"):
                return name, state, pid, entry
    return None


# ── ADRs ────────────────────────────────────────────────────────────────────────


def adr_dir(project: str) -> Path:
    return vault.project_path(project) / ADR_DIR


def list_adrs(project: str) -> list[dict]:
    folder = adr_dir(project)
    if not folder.is_dir():
        return []
    adrs = []
    for path in sorted(folder.iterdir()):
        match = _ADR_FILE.match(path.name)
        if not match:
            continue
        status_match = _ADR_STATUS.search(path.read_text(encoding="utf-8"))
        status = status_match.group(1).strip() if status_match else "?"
        adrs.append({
            "id": match.group(1),
            "title": match.group(2).strip(),
            "path": path,
            "status": status,
            "approved": "aprobado" in status.lower(),
        })
    return adrs


def _mark_adr_approved(project: str, adr_id: str, by: str) -> None:
    adr = next((a for a in list_adrs(project) if a["id"] == adr_id), None)
    if not adr:
        return
    text = adr["path"].read_text(encoding="utf-8")
    stamp = f"**Estado:** ✅ Aprobado por {by} el {datetime.now().astimezone():%d/%m/%Y}"
    if _ADR_STATUS.search(text):
        text = _ADR_STATUS.sub(lambda _: stamp, text, count=1)
    else:
        # Un ADR escrito sin el formato: la línea de estado va debajo del título.
        lines = text.splitlines()
        title_at = next((i for i, line in enumerate(lines) if line.startswith("# ")), -1)
        lines.insert(title_at + 1, f"\n{stamp}")
        text = "\n".join(lines) + "\n"
    adr["path"].write_text(text, encoding="utf-8")
    vault.commit_paths([vault.relative(adr["path"])], f"{adr_id}: aprobado por {by}")


# ── mesa técnica ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MesaStep:
    round: int
    role: str
    closing: bool = False


MESA_PARTICIPANTS = ("lider_tecnico", "dba", "producto")
_MESA_FILE_LABEL = {"lider_tecnico": "Lider tecnico", "dba": "DBA", "producto": "Producto"}
_MESA_FOCUS = {
    "lider_tecnico": (
        "Proponé el stack: lenguaje y framework del back (Java con Spring, Node con NestJS, "
        "Node con Express, Python con FastAPI u otro), el front (Angular por preferencia del "
        "usuario; otro solo con una razón concreta), la librería de componentes (Angular "
        "Material, PrimeNG, Spartan u otra) y la infraestructura de los dos ambientes que "
        "tiene todo proyecto: desarrollo (por defecto en la PC del usuario con Docker) y "
        "producción (dónde corre y cuánto cuesta). Para cada decisión, una tabla que "
        "compare al menos dos alternativas con criterios explícitos."
    ),
    "dba": (
        "Proponé el motor de base (PostgreSQL por preferencia del usuario; MySQL u otro solo "
        "con una razón concreta; NoSQL solo si el caso lo pide, y Redis como complemento, no "
        "como reemplazo) y un modelo de datos de alto nivel, con una base por ambiente "
        "(desarrollo y producción). Respondé a lo que propuso el Líder técnico."
    ),
    "producto": (
        "No elijas tecnologías: aportá lo que el negocio necesita que la decisión respete "
        "(usuarios, volumen esperado, plazos, presupuesto, integraciones, regulaciones) y "
        "señalá si alguna propuesta pone en riesgo el MVP."
    ),
}
ADR_TEMPLATE = """# ADR-NNN · <Título>

**Estado:** Propuesto
**Fecha:** <AAAA-MM-DD>
**Mesa:** [[<carpeta de la mesa>/Resumen]]

## Contexto
## Opciones evaluadas
| Criterio | Opción A | Opción B |
## Evidencia
## Decisión
## Consecuencias
"""


def mesa_plan(rounds: int) -> list[MesaStep]:
    steps = []
    for r in range(1, rounds + 1):
        participants = MESA_PARTICIPANTS if r == 1 else MESA_PARTICIPANTS[:2]
        steps += [MesaStep(r, role) for role in participants]
    steps.append(MesaStep(rounds, "lider_tecnico", closing=True))
    return steps


def quarantine_premature_adrs(project: str, folder: str, before: set[str]) -> list[str]:
    """ADRs escritos durante una ronda (no en el cierre) pasan a borradores de la mesa.

    En la primera prueba el Líder técnico escribió cinco ADRs en la ronda 1, sin el
    formato, y el cierre los volvió a escribir con otros números: diez ADRs duplicados.
    """
    moved = []
    for adr in list_adrs(project):
        if adr["path"].name in before:
            continue
        target = vault.project_path(project) / folder / f"Borrador - {adr['path'].name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        adr["path"].rename(target)
        vault.commit_paths(
            [vault.relative(adr["path"]), vault.relative(target)],
            f"mesa: {adr['id']} escrito antes del cierre, pasa a borrador",
        )
        moved.append(adr["id"])
    return moved


def adr_names(project: str) -> set[str]:
    return {a["path"].name for a in list_adrs(project)}


def mesa_folder(project: str, topic: str) -> str:
    """Carpeta nueva para una mesa, relativa a la carpeta del proyecto."""
    root = vault.project_path(project) / MESA_DIR
    existing = [p.name for p in root.iterdir()] if root.is_dir() else []
    number = 1 + max((int(n[:2]) for n in existing if n[:2].isdigit()), default=0)
    slug = re.sub(r"[^\w\s-]", "", topic).strip()[:50] or "Stack"
    return f"{MESA_DIR}/{number:02d} - {slug}"


def mesa_instruction(step: MesaStep, topic: str, folder: str, rounds: int) -> str:
    if step.closing:
        return f"""Cerrá la mesa técnica «{topic}». Leé todo lo que se escribió en la carpeta
«{folder}» y los documentos 00 y 01.

Si en esa carpeta hay archivos «Borrador - ADR-…», son borradores escritos antes de
tiempo: usalos como insumo, pero los ADRs definitivos los escribís vos ahora.

1. Escribí «{folder}/Resumen.md» con la propuesta final: qué se decidió, dónde hubo
   desacuerdo y cómo se resolvió.
2. Por cada decisión, un ADR en la carpeta «{ADR_DIR}» del proyecto, con nombre
   «ADR-NNN - Título.md», numerando a continuación de los que ya existan, con este
   formato exacto:

{ADR_TEMPLATE}
El estado va siempre «Propuesto»: lo aprueba el usuario. En «Evidencia» poné la
comparativa que justifica la decisión."""
    label = _MESA_FILE_LABEL[step.role]
    later = (
        "Leé las posturas de la ronda anterior y respondé: en qué coincidís, en qué no y por "
        "qué. Si cambiaste de opinión, decilo."
        if step.round > 1 else ""
    )
    return f"""Mesa técnica «{topic}», ronda {step.round} de {rounds}.

Leé los documentos 00 y 01 del proyecto y todo lo que ya se escribió en la carpeta
«{folder}». Escribí tu postura en «{folder}/R{step.round} - {label}.md» (creá ese
archivo; no modifiques los de los demás).

{_MESA_FOCUS[step.role]} {later}

Máximo una página. Las preferencias del usuario son sesgos, no imposiciones: si proponés
otra cosa, justificalo.

**No escribas ADRs ni el resumen**: eso se hace en el cierre de la mesa. En esta ronda,
solo tu postura en tu archivo."""


# ── textos para el orquestador y el usuario ─────────────────────────────────────


def summary_line(project: str) -> str:
    try:
        state = load(project)
    except (RuntimeError, ValueError):
        return f"{project} (estado ilegible)"
    if state is None:
        return f"{project} (anterior al proceso, sin fases)"
    line = f"{project}: fase {phase(state['fase']).label}"
    pending = [e["puerta"] for e in state["pendientes"].values()]
    if pending:
        line += f" (esperando aprobación de {', '.join(pending)})"
    return line


def status_text(project: str) -> str:
    state = load(project)
    if state is None and is_new_project(project):
        first = phase(1)
        return (
            f"«{project}» es un proyecto nuevo: todavía no existe. Arranca en la fase "
            f"{first.label} cuando delegues el primer trabajo. Qué hacer: {first.guide}"
        )
    if state is None:
        return f"«{project}» es anterior al proceso: trabaja sin fases ni puertas."
    current = phase(state["fase"])
    lines = [f"Proyecto «{project}»: fase {current.label}", f"Qué hacer ahora: {current.guide}"]
    if state["aprobaciones"]:
        lines.append("Aprobado: " + ", ".join(state["aprobaciones"]))
    if state["pendientes"]:
        lines.append("Esperando al usuario: " + ", ".join(
            e["puerta"] + (" (pidió cambios)" if e.get("esperando_cambios") else "")
            for e in state["pendientes"].values()
        ))
    adrs = list_adrs(project)
    if adrs:
        lines.append("ADRs: " + ", ".join(
            f"{a['id']} {'✅' if a['approved'] else '(propuesto)'}" for a in adrs
        ))
    return "\n".join(lines)


def after_approval_text(project: str, state: dict, gate: str, by: str, advanced: int | None) -> str:
    text = f"(Mensaje del sistema) {by} aprobó «{gate_label(gate, project)}» del proyecto «{project}»."
    if advanced:
        current = phase(advanced)
        text += f" El proyecto pasó a la fase {current.label}. Qué hacer ahora: {current.guide}"
    elif gate.startswith("ADR-"):
        pending = [a["id"] for a in list_adrs(project) if not a["approved"]]
        text += (
            f" Faltan aprobar: {', '.join(pending)}. Si ya pediste su aprobación, terminá el turno."
            if pending else
            " Todos los ADRs están aprobados: pedí la aprobación «stack»."
        )
    text += " Respondé corto: el usuario está en el celular."
    return text


def changes_text(project: str, gate: str, request_text: str) -> str:
    return (
        f"(Mensaje del sistema) El usuario pidió cambios en «{gate_label(gate, project)}» del "
        f"proyecto «{project}»: {request_text}\n\nDelegá las correcciones en el rol que "
        "corresponda y, cuando estén, volvé a pedir la aprobación."
    )


def after_mesa_text(project: str, topic: str, folder: str, adrs: list[dict]) -> str:
    listing = "\n".join(f"- {a['id']}: {a['title']}" for a in adrs) or "- (no se escribió ningún ADR)"
    return (
        f"(Mensaje del sistema) Terminó la mesa técnica «{topic}» de «{project}». Las posturas "
        f"y el resumen están en «{folder}». ADRs propuestos:\n{listing}\n\n"
        "Resumile al usuario la propuesta en pocas líneas y pedí la aprobación de cada ADR con "
        "orc-aprobacion (la puerta es el id del ADR). Después terminá el turno."
    )


# ── render del archivo de estado ────────────────────────────────────────────────


def render(project: str, state: dict) -> str:
    current = phase(state["fase"])
    rows = []
    for gate, number in GATES.items():
        approved = state["aprobaciones"].get(gate)
        waiting = any(e["puerta"] == gate for e in state["pendientes"].values())
        if approved:
            mark, when, who = "✅ Aprobado", _local(approved["fecha"]), approved["por"]
        elif waiting:
            mark, when, who = "⏳ Esperando al usuario", "", ""
        else:
            mark, when, who = "—", "", ""
        rows.append(f"| {phase(number).label} | {GATE_LABELS[gate]} | {mark} | {when} | {who} |")

    adrs = list_adrs(project)
    adr_rows = "\n".join(f"| [[{ADR_DIR}/{a['path'].stem}\\|{a['id']}]] | {a['title']} | {a['status']} |" for a in adrs)
    history = "\n".join(f"- {_local(h['fecha'])} · {h['evento']}" for h in state["historial"][-15:])
    data = json.dumps(state, ensure_ascii=False, indent=1)

    return f"""# 📍 Estado del Proyecto

> Lo mantiene el sistema a medida que avanza el proceso: no lo edites a mano.

**Fase actual:** {current.label}
**Qué sigue:** {current.guide}

---

## ⛩ Puertas de aprobación

| Fase | Puerta | Estado | Fecha | Por |
|------|--------|--------|-------|-----|
{chr(10).join(rows)}

---

## 📝 ADRs

{("| ADR | Título | Estado |" + chr(10) + "|-----|--------|--------|" + chr(10) + adr_rows) if adrs else "Todavía no hay ADRs."}

---

## 🕘 Historial

{history}

---

{_DATA_MARK}

```json
{data}
```
"""
