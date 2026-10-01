# Límites conocidos de Telegram — tg-sentry

> Estado: **v0.1.0**. Telegram NO publica límites oficiales para clientes MTProto;
> esto es experiencia acumulada de la comunidad + hallazgos del piloto real
> (protocolo interno del proyecto).

## Límites que respeta tg-sentry (por diseño)

| Parámetro | Default | Notas |
|---|---|---|
| Escrituras destructivas/min | 4 | Solo configurable a la baja; subir exige `--ack-over-limit` |
| Salir de grupos | ~30/h | Empírico; más es arriesgado |
| Bloquear usuarios | ~50/h | Empírico |
| Borrar mensajes (batch) | 1 paso = ≤100 msgs + cooldown proporcional | 12s por mensaje borrado (piso 60s, techo 20 min) → ~300 msgs/h; se OMITE si borró 0 o no queda ningún paso de borrado |
| Tope duro de sesión | 50 destructivas/h, 200/día | **Constante de código**, no configurable; al llenarse la cola **ESPERA** la ventana rodante (con countdown) — nunca aborta |
| FloodWait | se respeta SIEMPRE | Pausa bucket; gracia 2× espera tras terminar |
| Patrón de baneo | 2º FloodWait <5 min | Pausa indefinida + aviso; reanudación solo manual |

## Semánticas verificadas o documentadas

- **48h** (mensajes propios): borrar-para-todos solo dentro de las primeras
  48h; fuera de 48h el servidor degrada automáticamente a solo-para-ti
  (privados y grupos). El modo `all` nunca es error por edad. Modo `me`
  borra solo para ti, sin límite de edad, siempre. Operaciones distintas en
  `tg/ops.py` (`delete_own_messages(for_all=)`) y en la UI.
- **Salir vs borrar**: en grupos/canales `delete_dialog` = salir/darse de
  baja; el objeto sigue existiendo para los demás. El rol (creador/admin)
  exige `GetParticipant` por peer — mientras no se consulte, grupos y
  canales avisan «rol sin verificar: no se puede descartar que seas creador»
  (la exclusión por creador NO está activa hoy; ver `docs/security.md`).
- **Chats secretos**: Telethon no implementa E2E → detectar y marcar "no
  soportado", jamás operarlos como privados normales.
- **`revoke`** (eliminar chat privado): borra también del otro lado
  (sujeto a límites temporales del servidor).
- **`client.block` no existe en Telethon**: el bloqueo es API cruda
  (`BlockRequest`) — hallazgo del piloto.
- **`clear_history`** (vaciar historial SIN salir de un chat conservado):
  sin primitiva verificada en Telethon 1.x; el paso queda deshabilitado
  (`UnsupportedStep`) hasta investigar `messages.deleteHistory(just_clear)`
  en un piloto. La alternativa segura disponible: `--delete-own me --no-leave`
  (borra tus mensajes sin salir).

## ¿Cuánto cuesta limpiar mensajes propios? (la palanca honesta)

El default (12s/mensaje) es el envelope MÁS conservador. Contexto honesto:
borrar **tus propios mensajes** es la operación de MENOR riesgo de la
herramienta — eliminas tu contenido, no actúas sobre terceros, y los
clientes oficiales vacían historiales de cientos de mensajes al instante.
El riesgo serio de baneo vive en salir de grupos y bloquear en masa.

**Palanca**: `own_messages_cooldown_s` en el TOML (default 1200 = 300
msgs/h; mínimo 300 = 1200 msgs/h → un lote de 10 grupos pasa de ~3.5 h a
~50 min con el mínimo). Edítalo y reinicia la TUI; el ETA reflejará el
nuevo techo. Es
TU cuenta y TU riesgo: el default es prudente, el mínimo es ágil y ambos
respetan FloodWait y topes de sesión.

## Estrategia frente al baneo

El riesgo depende de **volumen y velocidad**, no de operaciones aisladas. El cooldown tras borrar mensajes propios es **proporcional a lo borrado** (12s por mensaje con el default: un grupo con 3 mensajes tuyos espera 60s, uno con 90 espera ~18 min) — mismo presupuesto de seguridad, menos esperas vacías (con el fijo histórico de 20 min, un lote de N grupos añadía 20·N minutos de pura espera).
tg-sentry lo afronta por capas: ritmo lento (4/min), caps por tipo, topes
duros de sesión, FloodWait con prioridad absoluta, cooldown extra para
borrados masivos, pausa indefinida ante patrón de baneo, y lotes de piloto
(`--limit-peers`) para crecer gradualmente. Nada de esto elimina el riesgo.

