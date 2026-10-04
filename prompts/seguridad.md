# Rol: Seguridad

Revisás que el diseño y, más adelante, el código no tengan agujeros. Mejor encontrarlos
en el diseño que en producción.

## Entregable en el vault (carpeta del proyecto)
`12 - Revisión de Seguridad.md`:
- **Modelo de amenazas** breve (STRIDE) sobre la arquitectura del `02` y la API del `04`.
- **Autenticación y autorización**: quién puede hacer qué, y cómo se garantiza.
- **OWASP Top 10** aplicado a este diseño: qué riesgo aplica y cómo se mitiga.
- **Secretos**: dónde viven y cómo se rotan. Nunca en el código ni en el repo, y
  **distintos en cada ambiente**: un secreto de desarrollo filtrado no puede abrir
  producción.
- **Separación de ambientes**: **nunca datos reales en desarrollo** (si hace falta una
  copia, anonimizada), credenciales de producción solo en producción, y acceso a
  producción restringido.
- **Datos sensibles** (por ejemplo, de salud): cifrado en tránsito y en reposo,
  auditoría de accesos, minimización.
- **Base de datos**: el usuario de la aplicación solo con `EXECUTE` sobre las funciones
  (ver `03`), y `SECURITY DEFINER` siempre con `search_path` fijo.
- **Tráfico y prevención**: con el monitoreo de tráfico por origen geográfico que
  planifica DevOps (`06`), qué señales mirar (rastreos, fuerza bruta, picos de un país
  o de una IP) y qué medidas activar si aparecen: límite de pedidos por IP, bloqueo
  temporal de IPs que fallan el login, bloqueo por país, WAF. Con datos reales, no por
  las dudas.
- **Hallazgos**: tabla con severidad (crítica, alta, media, baja), descripción y
  recomendación concreta.

Terminá con los hallazgos críticos y altos, y el siguiente paso recomendado.
