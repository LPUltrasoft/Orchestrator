"""Alta de cada proyecto en la autenticación compartida (decisión del usuario del 5/10/2026).

Todos los proyectos tienen login con usuario y contraseña, contra un solo servicio (el repo
LucianoPal/Autenticacion, desplegado en plataforma/autenticacion). Cada proyecto es una
**sociedad**, con sus roles y sus permisos. Producto los define en el 01, en la sección
«Roles y permisos», con un bloque ```json:

    {"permisos": [{"nombre": "turnos.AgendarTurno", "descripcion": "Agendar un turno",
                   "menu": {"nombre": "Turnos", "path": "/turnos"}}],
     "roles": [{"nombre": "Admin", "descripcion": "…", "admin": true, "permisos": ["turnos.AgendarTurno"]},
               {"nombre": "Paciente", "descripcion": "…", "autoregistro": true, "permisos": []}]}

Los permisos llevan el proyecto como prefijo (la tabla es una para todos). Un rol `admin`
recibe además los de administrar usuarios (RegistrarUsuarios, AsignarRoles,
RestablecerContrasena). El alta se puede repetir: deja la base igual al 01.

El gateway entra a la base como orc-ci (`sudo -u orc-ci orc-ci-docker exec …`), con el
JSON en una variable de psql: nada del documento se arma como SQL.
"""
from __future__ import annotations

import json
import re
import subprocess

from . import code, config, vault

DOC = "01 - Requerimientos Funcionales y Reglas de Negocio"
SECTION = "Roles y permisos"
ADMIN_PERMISSIONS = ("RegistrarUsuarios", "AsignarRoles", "RestablecerContrasena")
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
_ROLE_NAME = re.compile(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ ]{2,40}$")
_ACTION = r"[A-Z][A-Za-z0-9]{1,60}"


class AuthError(RuntimeError):
    pass


def prefix(project: str) -> str:
    return f"{code.slug(project)}."


def parse(text: str, project: str) -> dict:
    """El bloque de «Roles y permisos» del 01, validado. Lanza AuthError con lo que falta."""
    section = re.search(rf"^#+\s*{SECTION}\s*$", text, re.M | re.I)
    if not section:
        raise AuthError(f"el {DOC} no tiene la sección «{SECTION}»")
    block = _JSON_BLOCK.search(text, section.end())
    if not block:
        raise AuthError(f"la sección «{SECTION}» no tiene el bloque ```json")
    try:
        data = json.loads(block.group(1))
    except json.JSONDecodeError as exc:
        raise AuthError(f"el JSON de «{SECTION}» no es válido: {exc}") from exc

    problems = []
    pattern = re.compile(rf"^{re.escape(prefix(project))}{_ACTION}$")
    permisos = data.get("permisos") or []
    names = [p.get("nombre") for p in permisos if isinstance(p, dict)]
    for p in permisos:
        if not isinstance(p, dict) or not pattern.match(str(p.get("nombre", ""))):
            problems.append(f"permiso «{p.get('nombre') if isinstance(p, dict) else p}»: tiene que ser "
                            f"{prefix(project)}<Accion> (por ejemplo {prefix(project)}VerAgenda)")
            continue
        if not isinstance(p.get("descripcion"), str) or not 0 < len(p["descripcion"]) <= 200:
            problems.append(f"permiso {p['nombre']}: falta la descripción")
        menu = p.get("menu")
        if menu is not None and not (isinstance(menu, dict) and isinstance(menu.get("nombre"), str)
                                     and 0 < len(menu["nombre"]) <= 60
                                     and re.match(r"^/[\w\-/]{0,99}$", str(menu.get("path", "")))):
            problems.append(f"permiso {p['nombre']}: «menu» tiene que ser {{\"nombre\": …, \"path\": \"/…\"}}")
    if len(set(names)) != len(names):
        problems.append("hay permisos repetidos")

    roles = data.get("roles") or []
    if not roles:
        problems.append("no hay roles")
    for r in roles:
        if not isinstance(r, dict) or not _ROLE_NAME.match(str(r.get("nombre", ""))):
            problems.append(f"rol «{r.get('nombre') if isinstance(r, dict) else r}»: nombre de 2 a 40 letras")
            continue
        if not isinstance(r.get("descripcion"), str) or not 0 < len(r["descripcion"]) <= 200:
            problems.append(f"rol {r['nombre']}: falta la descripción")
        unknown = [n for n in r.get("permisos") or [] if n not in names]
        if unknown:
            problems.append(f"rol {r['nombre']}: permisos que no están en la lista: {', '.join(map(str, unknown))}")
        if r.get("admin") and r.get("autoregistro"):
            problems.append(f"rol {r['nombre']}: un rol admin no puede ser de autorregistro")
    role_names = [r.get("nombre") for r in roles if isinstance(r, dict)]
    if len(set(role_names)) != len(role_names):
        problems.append("hay roles repetidos")
    if not any(isinstance(r, dict) and r.get("admin") for r in roles):
        problems.append("falta un rol con \"admin\": true (administra los usuarios del proyecto)")
    if problems:
        raise AuthError("; ".join(problems))
    return {"permisos": permisos, "roles": roles}


def load(project: str) -> dict:
    path = vault.project_path(project) / f"{DOC}.md"
    if not path.exists():
        raise AuthError(f"falta el {DOC}")
    return parse(path.read_text(encoding="utf-8"), project)


# Todo en una transacción y repetible: sociedad, permisos del proyecto (los que ya no están
# se desactivan), roles (ídem) y permisos de cada rol (los que sobran se dan de baja).
_SQL = r"""
\set ON_ERROR_STOP on
begin;
select set_config('orc.datos', :'datos', true), set_config('orc.proyecto', :'proyecto', true),
       set_config('orc.idsociedad', :'idsociedad', true), set_config('orc.prefijo', :'prefijo', true),
       set_config('orc.admin', :'admin', true);
create temp table resultado (clave text, valor text) on commit drop;
do $$
declare
	d jsonb := current_setting('orc.datos')::jsonb;
	v_soc int := nullif(current_setting('orc.idsociedad'), '')::int;
	v_rol int;
	r jsonb;
	p jsonb;
	v_permisos text[];
begin
	if v_soc is null or not exists (select 1 from auth.sociedades where idsociedad = v_soc) then
		insert into auth.sociedades (descripcion) values (current_setting('orc.proyecto')) returning idsociedad into v_soc;
	end if;
	insert into resultado values ('idsociedad', v_soc::text);

	for p in select * from jsonb_array_elements(d->'permisos') loop
		insert into auth.permisos (nombre, descripcion, permisomenu, nombremenu, path, activo)
			values (p->>'nombre', p->>'descripcion', coalesce(jsonb_typeof(p->'menu') = 'object', false),
			        p->'menu'->>'nombre', p->'menu'->>'path', true)
			on conflict (nombre) do update set descripcion = excluded.descripcion,
				permisomenu = excluded.permisomenu, nombremenu = excluded.nombremenu, path = excluded.path,
				activo = true, fechaeliminacion = null, fechamodificacion = now();
	end loop;
	update auth.permisos set activo = false, fechamodificacion = now()
		where starts_with(nombre, current_setting('orc.prefijo')) and activo
		and nombre not in (select x->>'nombre' from jsonb_array_elements(d->'permisos') x);

	for r in select * from jsonb_array_elements(d->'roles') loop
		select idrol into v_rol from auth.roles where idsociedad = v_soc and nombre = r->>'nombre';
		if v_rol is null then
			insert into auth.roles (idsociedad, nombre, descripcion, autoregistro)
				values (v_soc, r->>'nombre', r->>'descripcion', coalesce((r->>'autoregistro')::boolean, false))
				returning idrol into v_rol;
		else
			update auth.roles set descripcion = r->>'descripcion', activo = true, fechaeliminacion = null,
				autoregistro = coalesce((r->>'autoregistro')::boolean, false), fechamodificacion = now()
				where idrol = v_rol;
		end if;
		v_permisos := array(select jsonb_array_elements_text(coalesce(r->'permisos', '[]'::jsonb)));
		if coalesce((r->>'admin')::boolean, false) then
			v_permisos := v_permisos || string_to_array(current_setting('orc.admin'), ',');
		end if;
		insert into auth.permisos_por_rol (idpermiso, idrol)
			select idpermiso, v_rol from auth.permisos where nombre = any(v_permisos)
			on conflict (idpermiso, idrol) do update set fechaeliminacion = null, fechamodificacion = now();
		update auth.permisos_por_rol set fechaeliminacion = now()
			where idrol = v_rol and fechaeliminacion is null
			and idpermiso not in (select idpermiso from auth.permisos where nombre = any(v_permisos));
		insert into resultado values ('rol:' || (r->>'nombre'), v_rol::text);
	end loop;
	update auth.roles set activo = false, fechamodificacion = now()
		where idsociedad = v_soc and activo
		and nombre not in (select x->>'nombre' from jsonb_array_elements(d->'roles') x);
end $$;
select clave || '=' || valor from resultado;
commit;
"""


def _as_ci(*args: str, input: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", "-u", "orc-ci", "orc-ci-docker", *args], input=input,
                          capture_output=True, text=True, timeout=timeout)


def apply(project: str, data: dict, idsociedad: int | None) -> dict:
    """Deja la sociedad del proyecto igual a `data`. Devuelve su id y el de cada rol."""
    result = _as_ci(
        "exec", "-i", config.AUTH_DEV_DB_CONTAINER, "psql", "-U", "postgres", "-d", "autenticacion", "-At",
        "-v", f"datos={json.dumps(data, ensure_ascii=False)}", "-v", f"proyecto={project}",
        "-v", f"idsociedad={idsociedad or ''}", "-v", f"prefijo={prefix(project)}",
        "-v", f"admin={','.join(ADMIN_PERMISSIONS)}",
        input=_SQL,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        error = next((line for line in detail.splitlines() if line.startswith(("ERROR:", "FATAL:", "psql:"))), detail[-400:])
        raise AuthError(f"la base de la autenticación no aceptó el alta: {error[:400]}")
    out = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {"idsociedad": int(out["idsociedad"]),
            "roles": {k.removeprefix("rol:"): int(v) for k, v in out.items() if k.startswith("rol:")}}


def create_admin(idsociedad: int, idrol: int, usuario: str = "admin") -> str | None:
    """El primer admin, con una contraseña al azar que se devuelve una sola vez. None si ya existe."""
    result = _as_ci("exec", config.AUTH_DEV_APP_CONTAINER, "node", "dist/scripts/crear-usuario.js",
                    usuario, str(idsociedad), str(idrol))
    if result.returncode != 0:
        if "ya existe" in result.stderr:
            return None
        raise AuthError(f"no pude crear el admin: {result.stderr.strip()[-300:]}")
    match = re.search(r"^Contraseña.*?: (\S+)$", result.stdout, re.M)
    if not match:
        raise AuthError("crear-usuario no devolvió la contraseña")
    return match.group(1)
