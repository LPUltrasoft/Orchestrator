#!/bin/sh
# Último paso del init de la base (solo la primera vez): el usuario con el que entra el
# servicio, con lo justo (no es dueño de nada ni superusuario), y los permisos para
# administrar usuarios, que después se asignan a los roles admin de cada proyecto.
set -eu
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v clave="$AUTH_DB_APP_PASSWORD" <<'SQL'
CREATE ROLE autenticacion_app LOGIN PASSWORD :'clave';
GRANT USAGE ON SCHEMA auth TO autenticacion_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA auth TO autenticacion_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA auth TO autenticacion_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA auth TO autenticacion_app;

INSERT INTO auth.permisos (descripcion, nombre) VALUES
	('Registrar usuarios de la sociedad con cualquier rol', 'RegistrarUsuarios'),
	('Asignar roles a usuarios de la sociedad', 'AsignarRoles'),
	('Restablecer la contraseña de usuarios de la sociedad', 'RestablecerContrasena')
	ON CONFLICT (nombre) DO NOTHING;
SQL
