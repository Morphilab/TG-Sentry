# Política de seguridad

## Reportar una vulnerabilidad

Si encuentras una vulnerabilidad de seguridad en tg-sentry (p. ej. filtración
de secretos en logs, omisión de salvaguardas, corrupción de la auditoría que
pueda provocar operaciones fuera de los límites), por favor **NO abras un
issue público**:

1. Usa los **advisories privados de GitHub** (*Security → Report a
   vulnerability*) del repositorio, o
2. Contacta al mantenedor por otro canal privado que habilite en la página
   del repositorio.

Responderemos y, confirmado el problema, publicaremos el fix y el advisory
coordinado.

## Alcance

- Filtración de secretos (api_id/api_hash, teléfono, session strings, códigos
  2FA) hacia logs, auditoría o exports.
- Cualquier ruta que permita operar sobre peers blindados (propia cuenta,
  777000, whitelist, chats secretos, chats donde eres creador, contactos
  protegidos).
- Fallos que permitan superar los topes anti-baneo (token bucket, topes de
  sesión 50/h y 200/día) o que corrompan la auditoría en dirección
  "contar de menos".

## Fuera de alcance

- Reportes sobre riesgo de **suspensión de cuenta** por uso de la herramienta:
  es un riesgo documentado y asumido (ver `README.md` y `docs/security.md`);
  no es una vulnerabilidad. tg-sentry aplica límites conservadores, pero
  ningún software puede garantizar que Telegram no suspenda la cuenta.
- Pérdida de datos por borrados confirmados: la herramienta es destructiva por
  diseño y exige confirmación tecleada; lo ejecutado es irreversible.

## Versiones con soporte

Solo la última versión publicada recibe correcciones de seguridad.
