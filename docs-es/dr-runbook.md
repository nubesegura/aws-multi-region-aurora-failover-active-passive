# Runbook de DR: simular la pérdida de us-east-2, promover us-west-2 y volver (failback)

Versión en inglés (fuente): [docs/dr-runbook.md](../docs/dr-runbook.md). Esta es una copia en español; si difieren, manda el inglés.

> **Notas de honestidad.**
> 1. El simulacro **simula** una falla regional limitando a cero la concurrencia de las funciones de `us-east-2`. No
>    reproduce una caída regional real: en una real también desaparecen la base de datos, la red y el plano de control de la
>    región. La simulación es reversible; un failover real no lo es.
> 2. La replicación de Aurora Global Database es **asíncrona**. El retraso habitual es de aproximadamente un segundo, así
>    que un failover no planificado puede perder las escrituras de más o menos el último segundo. Es un valor típico, no
>    una garantía. Un switchover planificado no pierde nada.
> 3. **El simulacro planificado se ejecutó en AWS dos veces**: a mano el 2026-10-14 y, con el runner automatizado, el 2026-11-11 (ver
>    [Valores observados](#valores-observados) y el [informe de pruebas del simulacro](dr-test-report.md)).
>    El **camino no planificado (U1 a U8) y la reconstrucción manual de un secundario (U7) nunca se ejecutaron**: se escribieron a
>    partir de la documentación de AWS. Hazlos antes en una cuenta desechable.
> 4. El objetivo de RTO es de 15 minutos. En la única ejecución medida, la del 2026-11-11, el RTO de escritura fue de 196,8 s hacia el
>    secundario y de 82,7 s de vuelta al primario (todo el simulacro, de la línea base a la comprobación del ledger, duró unos 12 minutos);
>    una ejecución no es una garantía.
> 5. La protección contra borrado está **activada** de nuevo (`DeletionProtection` es `true` en `parameters/prod.json`, restaurada por
>    el commit `9c40f1b`); el procedimiento de destrucción la desactiva primero, tras una confirmación escrita.
> 6. Los valores del 2026-10-14 de más abajo son historia de la **época de la API key**. Desde la spec 002 la API usa ID tokens
>    Bearer de Cognito y el workflow `failover` también activa y desactiva la réplica de Cognito (unos 40 s por dirección). La medición
>    vigente es el simulacro del 2026-11-11 del [informe de pruebas](dr-test-report.md).

## Hoja de referencia rápida

### Simulacro planificado con los workflows (ambas regiones sanas, sin pérdida de datos)

Se lanza desde GitHub: *Actions > simulate-outage* y *Actions > failover*, rama `main`, Environment `prod`. Usan los mismos scripts
que la tabla siguiente, se detienen en el primer paso que falla y escriben un resumen del job (quién, segundos por paso, región del
writer antes y después, resultado de consistencia). Ningún workflow pasa nunca `--force`, `--unplanned` ni `FORCE_UNFENCE`.

| # | Ejecución del workflow | Entradas | Resultado esperado | Cómo deshacer |
|---|---|---|---|---|
| 0 | `simulate-outage` | `action` = `status`, `region` = `us-east-2` | Lista las seis funciones sin fence | Nada (solo lectura) |
| 1 | `simulate-outage` | `action` = `fence`, `region` = `us-east-2`, `confirm` = `us-east-2` | Concurrencia reservada 0 en las seis funciones de us-east-2; la app bancaria empieza a reintentar | `simulate-outage` con `unfence` |
| 2 | `failover` | `direction` = `to-secondary`, `confirm` = `failover` | Exige us-east-2 con fence y us-west-2 sin fence; **activa la réplica de Cognito en us-west-2** (`ReplicaAttachmentStatus=ACTIVE`); switchover planificado; pesos 0 / 255; validación (comprobación de la región que responde con `GET /health?deep=true`); termina en verde cuando todos los pasos pasan | `failover` con `to-primary` |
| 3 | `failover` | `direction` = `to-primary`, `confirm` = `failover` | Levanta el fence de us-east-2, devuelve la base de datos, **desactiva la réplica de Cognito** (`ReplicaAttachmentStatus=INACTIVE`), pesos 255 / 0, validación | `failover` con `to-secondary` |

Notas sobre los workflows:

- `simulate-outage`, `failover`, `deploy` y `destroy` comparten el grupo de concurrencia `drdemo-prod-infrastructure`: nunca corren
  a la vez. GitHub conserva solo una ejecución **pendiente** por grupo, así que un `deploy` en cola puede reemplazar en silencio un
  `failover` en cola (y al revés). Tras lanzarlo, abre la lista de ejecuciones y confirma que la tuya arrancó.
- Los workflows necesitan las variables del Environment `prod` (`AWS_REGION_PRIMARY`, `AWS_REGION_SECONDARY`, `AWS_REGION_GLOBAL`,
  `API_DOMAIN_NAME`, `HOSTED_ZONE_ID`) y el secreto `AWS_ROLE_ARN`. No guardan ningún token ni contraseña de Cognito. Sin token, el
  workflow valida solo la región que responde con `GET /health?deep=true`: la línea base de `/stats`, el depósito de prueba de 0,01 y la
  verificación de consistencia del ledger (`45-consistency.sh`) se **omiten** en el workflow (el paso imprime que lo omitió). Ejecuta
  tú `bash scripts/dr/45-consistency.sh` con un token después (ver abajo) para probar el ledger.
- Un paso que falla deja el sistema como está. Lee el resumen (región del writer antes y después) y el log antes de lanzar de nuevo.
- `failover` hacia el secundario también se niega a empezar si la región secundaria tiene fence (levántalo antes).

### Simulacro planificado con los scripts (ambas regiones sanas, sin pérdida de datos)

Ejecuta desde la raíz del repositorio con **tus credenciales de propietario**. Variables que debes exportar antes:
`AWS_REGION_PRIMARY=us-east-2`, `AWS_REGION_SECONDARY=us-west-2`, `AWS_REGION_GLOBAL=us-east-1`, `API_DOMAIN_NAME=api.example.com`.
Las rutas protegidas necesitan un **ID token** de Cognito de un usuario demo (`bank-operator` o `dashboard-viewer`, creados por
`.github/workflows-local/provision-users/provision_users.py`): expórtalo como `COGNITO_ID_TOKEN`. Vale 15 minutos, así que obtén uno
nuevo antes de cada validación (inicia sesión en la app bancaria, o llama a `InitiateAuth` de Cognito con `USER_PASSWORD_AUTH` contra el
cliente de la app bancaria; nunca pegues una contraseña o un token en un log compartido). `45-consistency.sh` lo exige; `40-validate.sh`
lo usa para `/stats` y la prueba y sin él solo comprueba `GET /health?deep=true`; `00-precheck.sh` registra la línea base de `/stats`
solo cuando `API_DOMAIN_NAME` y `COGNITO_ID_TOKEN` están exportadas. Los scripts nunca imprimen el token. Todos los scripts aceptan
`--dry-run` y `--help`.

**Réplica de Cognito en el camino con scripts.** El workflow `failover` activa la réplica en us-west-2 antes del switchover y la
desactiva en `to-primary`; los scripts sueltos de la tabla siguiente **no** tocan Cognito. Si ejecutas los pasos a mano, activa la réplica
justo antes del paso 2 y desactívala después del paso 5a, con el mismo helper del workflow (necesita el rol de ejecución de CloudFormation
en `CFN_EXEC_ROLE_ARN` cuando no usas el rol de propietario):

```bash
bash -c 'set -euo pipefail; source scripts/lib/common.sh; source scripts/lib/dr.sh; cognito_replica_set_status ACTIVE'    # o INACTIVE
```

Actualiza el stack `cognito` de us-west-2 con `ReplicaAttachmentStatus=<ACTIVE|INACTIVE>` y conserva sus demás parámetros. El runner
local `.github/workflows-local/failover.sh` lo hace por ti.

| # | Paso | Comando | Resultado esperado | Cómo verificar | Cómo deshacer |
|---|---|---|---|---|---|
| 0 | Verificaciones previas | `bash scripts/dr/00-precheck.sh` | us-east-2 writer, us-west-2 reader, lag menor de 5000 ms, sin fence, pesos 255/0, se imprime la línea base de `/stats` (necesita `COGNITO_ID_TOKEN`) | Código de salida 0 | Nada (solo lectura) |
| 1 | Simular la pérdida de us-east-2 | `bash scripts/dr/10-simulate-outage.sh` | Concurrencia reservada 0 en las 6 funciones de us-east-2 | La app bancaria muestra "recovering", el dashboard muestra Unavailable; `40-validate.sh` falla (`10-simulate-outage.sh --status` informa el fence) | `bash scripts/dr/10-simulate-outage.sh --undo` |
| 2 | Promover us-west-2 (switchover planificado) | `bash scripts/dr/20-promote.sh` | `switchover finished: us-west-2 is the writer. No data was lost.` | `bash scripts/dr/00-precheck.sh` muestra los roles intercambiados | `bash scripts/dr/50-failback-promote.sh` |
| 3 | Cambiar Route 53 a us-west-2 | `bash scripts/dr/30-switch-traffic.sh` | Stack dns actualizado, pesos 0 / 255 | `bash scripts/dr/40-validate.sh --expect-region us-west-2 --wait 180 --probe` | `bash scripts/dr/30-switch-traffic.sh --to primary` (solo mientras us-east-2 sea el writer) |
| 4 | Validar | App bancaria, dashboard, carpeta Postman `04 DR drill`, `40-validate.sh --probe`, `bash scripts/dr/45-consistency.sh` | `served_by_region` us-west-2, `database_role` writer, saldo y conteos no menores que la línea base, ledger consistente | `validation passed`, `consistent: true` (salida 0) | n/a |
| 5a | Failback: base de datos | `bash scripts/dr/50-failback-promote.sh` | us-east-2 vuelve a ser el writer | `bash scripts/dr/00-precheck.sh` | `bash scripts/dr/20-promote.sh` |
| 5b | Failback: fence y tráfico | `bash scripts/dr/60-restore.sh` | Fence levantado, pesos 255 / 0 | `bash scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe` | `bash scripts/dr/10-simulate-outage.sh` y `bash scripts/dr/30-switch-traffic.sh --to secondary` |
| 6 | Revisión del estado final | `bash scripts/dr/00-precheck.sh` | Vuelve el estado normal | Código de salida 0 | n/a |

### Camino no planificado (caída regional real, posible pérdida de datos)

| # | Paso | Comando | Resultado esperado | Cómo verificar | Cómo deshacer |
|---|---|---|---|---|---|
| U1 | Registrar lo que se sabe | `bash scripts/dr/00-precheck.sh` (puede reportar problemas de us-east-2) | Última línea base, pesos, roles en vivo | Anota el último retraso de replicación (CloudWatch, us-west-2) | Nada |
| U2 | Fence de us-east-2 y luego promover con pérdida de datos | `bash scripts/dr/20-promote.sh --unplanned` | Confirmaciones escritas y luego `failover finished: us-west-2 is the writer` | Lista de miembros en la salida; `00-precheck.sh` | **Ninguno.** No es reversible |
| U3 | Cambiar Route 53 | `bash scripts/dr/30-switch-traffic.sh` | Pesos 0 / 255, región antigua con fence | `bash scripts/dr/40-validate.sh --expect-region us-west-2 --wait 180 --probe` | Solo después de una reconstrucción, ver U8 |
| U4 | Validar | App bancaria, dashboard, Postman carpeta `04`, `40-validate.sh --probe`, `45-consistency.sh` | us-west-2 atiende lecturas y escrituras | `validation passed`; el informe de consistencia dice qué, si algo, es inconsistente | n/a |
| U5 | Medir la brecha de datos | Comparar la línea base con `/stats` (sección U5) | Número de escrituras que faltan | Nota escrita | n/a |
| U6 | Mantener us-east-2 con fence | `bash scripts/dr/10-simulate-outage.sh` cuando la región responda de nuevo | Fence puesto | `bash scripts/dr/00-precheck.sh` muestra concurrencia reservada 0 | Solo después de U8 (`60-restore.sh`) |
| U7 | Reconstruir un secundario en us-east-2 | Pasos manuales con AWS CLI (sección U7) | El nuevo clúster entra al clúster global como reader | `describe-global-clusters`, `00-precheck.sh` | Borrar el clúster nuevo |
| U8 | Failback | `bash scripts/dr/50-failback-promote.sh` y luego `bash scripts/dr/60-restore.sh` | Estado normal | `bash scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe` | Ver pasos 5a y 5b |

### Confirmaciones escritas y sobrescrituras

| Dónde | Frase o variable | Significado |
|---|---|---|
| `20-promote.sh --unplanned` | escribir `allow-data-loss us-west-2` | Aceptar que las últimas escrituras pueden perderse. Siempre se escribe en una terminal interactiva: no hay flag ni variable para ello, así que no puede ejecutarse desde un trabajo no interactivo |
| `20-promote.sh --unplanned`, `30-switch-traffic.sh` | escribir `proceed-unfenced us-east-2` o poner `PROMOTE_UNFENCED_CONFIRM` con esa frase | Continuar aunque no se pudo hacer fence de la región antigua (su API no responde) |
| `10-simulate-outage.sh --undo`, `60-restore.sh` | escribir `unfence-standalone <región>` o poner `FORCE_UNFENCE` con esa frase | Levantar el fence en un clúster que **no** es miembro del clúster global (puede aceptar escrituras: split brain) |
| `20-promote.sh`, `50-failback-promote.sh` | `--yes` | Omitir la pregunta y/N de un switchover planificado |
| `10-simulate-outage.sh --undo`, `60-restore.sh` | `RESERVED_CONCURRENCY=<n>` | Restaurar una reserva concreta en lugar de quitarla (la plantilla no define ninguna) |
| `30-switch-traffic.sh`, `60-restore.sh` | `CFN_EXEC_ROLE_ARN` | Pasar el rol de ejecución de CloudFormation (`--role-arn`) al actualizar el stack dns (el workflow `failover` lo lee del stack bootstrap global) |
| Workflow `simulate-outage`, `action` = `fence` | `confirm` = el nombre de la región | Confirmación escrita para cada fence |
| Workflow `failover` | `confirm` = `failover` | Confirmación escrita para cualquier dirección |

## Valores observados

### Simulacro medido (2026-11-11, vigente)

Un simulacro planificado automatizado, de ida y vuelta, sobre el demo desplegado con autenticación Cognito (una ejecución, no estadísticas).
Tablas completas, método y límites: [informe de pruebas del simulacro](dr-test-report.md). Veredicto PASS.

| Medida | Observado |
|---|---|
| RTO de escritura, to-secondary | 196,8 s (escrituras no disponibles 196,2 s) |
| RTO de escritura, to-primary | 82,7 s (escrituras no disponibles 82,5 s) |
| Workflow `failover`, total | 223 s to-secondary, 236 s to-primary (cada uno incluye unos 40 s del paso de la réplica de Cognito) |
| RPO medido (switchover planificado) | 0 escrituras confirmadas perdidas: 504 movimientos almacenados, 504 esperados |
| Retraso de replicación durante la ejecución | `AuroraGlobalDBReplicationLag` máximo 933 ms; `AuroraGlobalDBRPOLag` máximo 1531 ms |

El RTO de escritura es el tiempo en que la sonda de escritura no obtuvo una escritura confirmada desde la región nueva. **No es
comparable** con los 195 s del 2026-10-14 de más abajo, que eran la duración total del workflow `failover`. No cites la convergencia DNS del
informe (negativa en ambas direcciones: la respuesta DNS cambió antes de que terminara el paso de cambio de tráfico) ni la disponibilidad del
inicio de sesión en el secundario (70 % y 43 %, en parte por diseño porque el workflow activa y desactiva la réplica). No ejercitado: el
failover no planificado.

### Valores observados (2026-10-14, historia)

Un simulacro planificado, de ida y vuelta, sobre el demo desplegado con la autenticación por **API key** de la spec 001 (valores de las
ejecuciones de los workflows; una ejecución cada uno, no estadísticas). Se conservan como historia y se midieron con otra métrica (duración
total del workflow); los valores vigentes están arriba:

| Paso | Observado |
|---|---|
| Fence de us-east-2 (`simulate-outage`) | 41 s |
| `failover` to-secondary, total | 195 s (switchover planificado 36 s, cambio de pesos DNS unos 70 s) |
| `failover` to-primary, total | 170 s (switchover planificado 35 s) |
| Escrituras no disponibles por switchover | unos 35 a 40 s |
| Ledger tras ambos movimientos | consistente (`45-consistency.sh`); ningún movimiento confirmado se perdió |

No observado: el failover no planificado, la medición de la brecha de datos (U5), la reconstrucción de un secundario (U7), el tiempo
que tardan resolvers y conexiones keep-alive en seguir un cambio de pesos en un cliente que no controlas, y una ejecución con retraso
de replicación distinto de cero.

## Por qué enrutamiento ponderado manual y no enrutamiento de failover con health checks

El demo usa dos registros alias ponderados de Route 53 (pesos 255 y 0) que cambia una persona. Se consideró y se descartó el
enrutamiento de failover automático con health checks:

- **La base de datos debe promoverse antes de mover el tráfico.** El enrutamiento de failover cambia el DNS en cuanto
  falla un health check. La base de datos de la región secundaria es una copia de solo lectura hasta que alguien la
  promueve, así que toda escritura enviada allí respondería `503 database_read_only`. El tráfico debe seguir a la
  promoción, nunca precederla.
- **Promover es una decisión humana.** Un failover no planificado (`--allow-data-loss`) puede perder escrituras y separa
  para siempre al primario antiguo. Un health check no puede decidir eso.
- **Split brain.** Tras un failover no planificado el primario antiguo todavía puede aceptar escrituras. Hay que hacer
  fence de la región antigua (funciones limitadas a cero) antes de mover tráfico; los health checks no saben nada de eso.
- **Falsas alarmas y costo.** Un health check inestable movería a los clientes a una región de solo lectura; además los
  health checks cuestan dinero.
- **Revertir es trivial** con pesos (255 / 0 y de vuelta), y la misma actualización de stack funciona aunque `us-east-2`
  esté caída porque se ejecuta en `us-east-1`.

El workflow `failover` automatiza exactamente ese orden para el caso **planificado** (comprobación del fence, switchover, espera al
writer y solo entonces los pesos), lanzado por una persona. Una versión guiada por health checks sigue fuera de alcance. Ver
[ADR 0003](../docs/adr/0003-manual-weighted-routing.md) y [ADR 0015](../docs/adr/0015-planned-switchover-only.md).

## Convenciones de los scripts

- Vocabulario: "región primaria" y "región secundaria" son las regiones **de la creación** (`AWS_REGION_PRIMARY` es
  `us-east-2`, `AWS_REGION_SECONDARY` es `us-west-2`). Tras una promoción el writer real es la otra; todos los scripts
  leen los roles en vivo de `describe-global-clusters` y nunca lo suponen.
- Cada script imprime la cuenta (enmascarada), las regiones y acciones con marca de tiempo (UTC). `--help` imprime su
  cabecera, que documenta cómo deshacerlo. `--dry-run` no llama a ninguna API de AWS: úsalo para ensayar.
- Los stacks de base de datos (`stk-useast2-drdemo-database-prod`, `stk-uswest2-drdemo-database-prod`) **nunca se vuelven
  a desplegar durante ni después de un simulacro**. RDS intercambia los roles; CloudFormation mostrará los roles previos al
  intercambio (drift) por diseño ([ADR 0005](../docs/adr/0005-database-stacks-not-redeployed-during-drills.md)).
  `scripts/deploy.sh` se niega a tocarlos mientras los roles estén intercambiados.
- El `lag_gate` de un switchover planificado (`scripts/lib/promote.sh`) espera hasta 5 minutos el primer dato de
  `AuroraGlobalDBReplicationLag` de un clúster que acaba de pasar a secundario (CloudWatch lo publica 2 a 3 minutos después de un
  switchover) y luego falla cerrado: sin dato, o con una consulta fallida, detiene el paso antes de cambiar nada. Ir al secundario y
  volver enseguida espera, por diseño.
- Las funciones se conectan al **endpoint del clúster** de su región, que es reader antes de una promoción y writer
  después. No se redespliega nada en ninguna región durante un simulacro. Una función caliente que mantenía una conexión
  con el writer antiguo la descarta al primer error y reintenta una vez con una conexión nueva, y `database_role` siempre
  se lee en una conexión nueva ([ADR 0013](../docs/adr/0013-connection-discard-and-single-retry.md)); la primera solicitud
  tras una promoción puede ser algo más lenta, pero no debería informar un rol desactualizado.
- Notas de despliegue que importan durante un simulacro: los workflows de deploy y destroy nunca tocan el stack bootstrap global (se aplica
  a mano con un change set, [ADR 0016](../docs/adr/0016-organization-github-role-and-account-baseline.md)); `templates/bootstrap.yaml` está
  por debajo del límite de 51.200 bytes en línea de CloudFormation, y `scripts/deploy.sh` envía por el bucket de artefactos de su región
  cualquier plantilla más grande.

## Paso 0. Requisitos previos y verificaciones

Requisitos:

- El demo está desplegado y sano ([README](../README.md#deploy)); el dashboard de monitoreo funciona y muestra `us-east-2` / `writer`.
- Para los **workflows**: las variables del Environment `prod` y el secreto `AWS_ROLE_ARN` del README, y los dos usuarios
  demo en el user pool de Cognito (creados una vez con `.github/workflows-local/provision-users/provision_users.py`).
- Para los **scripts y el camino no planificado**: credenciales de propietario con permiso para leer RDS, CloudWatch, Lambda y
  CloudFormation, para llamar a `rds:SwitchoverGlobalCluster` / `rds:FailoverGlobalCluster`, `lambda:PutFunctionConcurrency` y
  `lambda:DeleteFunctionConcurrency`, para actualizar el stack dns (CloudFormation y Route 53) y para actualizar el stack `cognito` de la región réplica (la activación de la réplica).
- Herramientas: AWS CLI v2, `bash` 4+, `date` de GNU, `jq`, `curl`.
- Variables exportadas: `AWS_REGION_PRIMARY`, `AWS_REGION_SECONDARY`, `AWS_REGION_GLOBAL`, `API_DOMAIN_NAME` (y `COGNITO_ID_TOKEN` para las comprobaciones autenticadas).
- Postman importado con `baseUrl` (`https://api.example.com`) y `cognito_id_token` (secreto) definidos; la app bancaria y el dashboard de
  monitoreo abiertos en dos pestañas, con sesión iniciada como `bank-operator` y `dashboard-viewer`.
- Un ensayo corto: `bash scripts/dr/00-precheck.sh --dry-run` y lo mismo con los demás scripts.

**Comando**

```bash
bash scripts/dr/00-precheck.sh
```

**Resultado esperado.** Cinco secciones numeradas: (1) los miembros del clúster global con sus roles en vivo
(`us-east-2 ... writer`, `us-west-2 ... reader`, `sync=...`); (2) ambos clústeres con `status=available`; (3) el retraso de
replicación del reader según CloudWatch (`AuroraGlobalDBReplicationLag`, milisegundos) y la lectura de RPO; (4) la
concurrencia reservada de las seis funciones en cada región (`unreserved`) y los pesos de Route 53
(`us-east-2 (record use2)=255  us-west-2 (record usw2)=0`); (5) una línea base de `/stats` (`served_by_region`,
`database_role`, `total_count`, `counts_by_region`, `last_processed_at_by_region`), que se imprime solo si `API_DOMAIN_NAME` y
`COGNITO_ID_TOKEN` están exportadas. **Anota la línea base** (y el saldo que muestra la app bancaria): es como medirás la pérdida de
datos más adelante.

**Cómo verificar.** Código de salida 0 y `precheck finished`. El script sale con 1 cuando un clúster no está `available`
o el lag supera 5000 ms (entonces no inicies un switchover planificado). En el dashboard de monitoreo la región activa es `us-east-2`
y el rol `writer`. Ejecuta una vez la carpeta Postman `04 DR drill`; las cuatro solicitudes pasan y `X-Served-By-Region`
es `us-east-2`.

**Cómo deshacer.** Nada: es de solo lectura.

## Simulacro planificado

### Paso 1. Simular la pérdida de us-east-2

Esto pone concurrencia reservada 0 en las seis funciones de `us-east-2` (`create-transaction`, `list-transactions`,
`get-stats`, `get-health`, `get-account`, `get-consistency`). Antes comprueba que las seis existen y, si no, no cambia nada.
Con el workflow: `simulate-outage`, `action` = `fence`, `region` = `us-east-2`, `confirm` = `us-east-2`. Lambda rechaza entonces toda invocación, de modo que toda solicitud que Route 53 siga enviando
a `us-east-2` falla. La base de datos, API Gateway y el DNS de `us-east-2` siguen funcionando: **esto simula, no
reproduce, una falla regional.**

**Comando**

```bash
bash scripts/dr/10-simulate-outage.sh
```

(`--region <región>` apunta a otra región, `--status` solo informa; la región por defecto es `AWS_REGION_PRIMARY`.)

**Resultado esperado.** `simulating the loss of us-east-2 ...`, `fence set in us-east-2` y después seis líneas
`us-east-2 <función>: reserved concurrency 0`.

**Cómo verificar.** La app bancaria muestra "recovering, attempt N" en una operación; el dashboard pasa al aviso "Unavailable" y sigue reintentando; las solicitudes de Postman fallan;
`bash scripts/dr/40-validate.sh` termina con `validation FAILED`. Las alarmas de CloudWatch `...lambda-throttles...` y
`...api-5xx...` de `us-east-2` pasan a ALARM (llega un correo si `ALERT_EMAIL` está definido): es la señal esperada.

**Cómo deshacer.** `bash scripts/dr/10-simulate-outage.sh --undo` (workflow: `action` = `unfence`) quita la reserva (define `RESERVED_CONCURRENCY=<n>`
para restaurar un valor concreto). Antes comprueba que el clúster de esa región sea miembro del clúster global; para un
clúster separado pide la frase escrita (ver la hoja de referencia).

### Paso 2. Promover us-west-2 (switchover planificado)

Aurora espera a que el secundario se ponga al día y luego intercambia los roles sin pérdida de datos. Las escrituras se
pausan un rato corto (unos 35 a 40 s en la ejecución observada). Con los workflows, los pasos 2 y 3 son una sola ejecución de
`failover` con `direction` = `to-secondary`, que antes activa la réplica de Cognito en us-west-2 (`ReplicaAttachmentStatus=ACTIVE`) para
que la región secundaria pueda autenticar usuarios mientras sirve el tráfico.

**Comando**

```bash
bash scripts/dr/20-promote.sh
```

Opciones: `--max-lag-ms N` (por defecto 5000), `--force` (ignora el límite de lag y un dato ausente; solo en terminal), `--yes` (omite la pregunta y/N),
`--api-region <región>` (la región cuya API de RDS se llama; por defecto la del writer antiguo, usa
`--api-region us-west-2` si esa API no responde).

**Resultado esperado.** La lista de miembros, `replication lag of the cluster in us-west-2: <n> ms (limit 5000 ms)`, la
pregunta y/N (`Planned switchover: us-west-2 becomes the writer, us-east-2 becomes a reader ...`), luego líneas de sondeo
`us-west-2: reader (check 1 of 80)` que terminan en `us-west-2: writer`, y
`switchover finished: us-west-2 is the writer. No data was lost.` El script espera hasta 20 minutos el cambio de rol y, antes de
la pregunta, hasta 5 minutos el primer dato de lag de un secundario nuevo (`no replication lag datapoint for ... yet ... waiting 30 s`).

**Cómo verificar.** `bash scripts/dr/00-precheck.sh` ahora lista `us-west-2 ... writer` y `us-east-2 ... reader` (el script
lee los roles en vivo). El dashboard sigue sin disponibilidad: el DNS aún apunta a la región con fence. **No** vuelvas a
desplegar los stacks de base de datos.

**Cómo deshacer.** `bash scripts/dr/50-failback-promote.sh` (el switchover espejo de vuelta a `us-east-2`).

### Paso 3. Cambiar Route 53 a us-west-2

Mueve los pesos de 255 / 0 a 0 / 255 actualizando el stack dns en `us-east-1`. No necesita `us-east-2`.

**Comando**

```bash
bash scripts/dr/30-switch-traffic.sh
```

(`--to secondary` es el valor por defecto; `--force` omite la comprobación "el destino es el writer" y es solo para pruebas.)

**Resultado esperado.** `goal: send all traffic to us-west-2`, los pesos actuales y nuevos, `the cluster in us-east-2 is a
reader of the same global cluster ...` (un clúster antiguo separado o desconocido recibiría fence primero), el despliegue de
CloudFormation de `stk-useast1-drdemo-dns-prod`, `dns weights: us-east-2 (record use2)=0  us-west-2 (record usw2)=255` y
`done. New requests reach us-west-2 within about 60 s (TTL) plus resolver and keep-alive caching.`

El script se niega a mover tráfico a una región cuyo clúster no sea el writer; promueve primero.

**Cómo verificar**

```bash
bash scripts/dr/40-validate.sh --expect-region us-west-2 --wait 180 --probe
```

Sondea `/stats` cada 10 segundos hasta 180 segundos, hasta que `served_by_region` sea `us-west-2`; luego imprime
`database_role`, la cabecera `X-Served-By-Region`, los conteos, llama a `/health?deep=true` y publica una prueba: un **depósito de 0,01**
(no existe API para borrar, así que la cuenta del demo conserva ese centavo; un retiro posterior de 0,01 lo compensa). Esperado:
`validation passed`. Luego revisa el ledger:

```bash
bash scripts/dr/45-consistency.sh
```

Salida 0 significa `consistent: true` (saldo recalculado y almacenado iguales, cadena íntegra, ninguna clave duplicada); 1 significa
inconsistente o sin respuesta válida; 2 significa que el ledger supera 50.000 filas y no se afirma nada.

**Cómo deshacer.** `bash scripts/dr/30-switch-traffic.sh --to primary`. Solo es válido mientras el clúster de `us-east-2`
sea el writer; si los roles están intercambiados, deshaz primero el paso 2, porque mover tráfico a un reader respondería
`503 database_read_only`.

### Paso 4. Validar en el dashboard y con Postman

1. Dashboard de monitoreo: la región activa cambia a `us-west-2` y el cambio se resalta en el aviso y en la línea de tiempo; el rol
   de base de datos es `writer`; el saldo y el total son al menos la línea base (un switchover planificado no pierde nada) y los
   movimientos antiguos siguen listados bajo `us-east-2`. Pulsa **Verify**: el informe es consistente.
   App bancaria: ejecuta un depósito; lo procesa `us-west-2` y el saldo crece. **Verify my operations** informa que no falta ninguno.
2. Carpeta Postman `04 DR drill`: `Drill: where am I served from (stats)`, `Drill: health deep (region and role)`,
   `Drill: write probe` y `Drill: header matches body region` pasan. `served_by_region` es `us-west-2`.
3. Algunos clientes aún pueden llegar a `us-east-2` durante un tiempo (cachés de resolvers, conexiones keep-alive que
   ignoran el DNS): recarga la pestaña del dashboard si se queda en la región antigua. Reciben errores, no divergencia de
   datos, porque `us-east-2` tiene fence y su base de datos ahora es reader.

**Cómo deshacer.** No hay nada que deshacer; la validación solo añade una fila de prueba si pediste `--probe`.

### Paso 5. Volver a us-east-2 (failback)

El failback es el espejo del camino de ida. Requiere el camino planificado anterior: el clúster de `us-east-2` debe ser un
miembro reader del clúster global.

**5a. Promover de nuevo al primario original**

```bash
bash scripts/dr/50-failback-promote.sh
```

Mismas opciones y salida que el paso 2, en espejo: `switchover finished: us-east-2 is the writer. No data was lost.`
Verifica con `bash scripts/dr/00-precheck.sh`. Deshacer: `bash scripts/dr/20-promote.sh`.

**5b. Levantar el fence y devolver el tráfico**

```bash
bash scripts/dr/60-restore.sh
```

Comprueba que el clúster de `us-east-2` es el writer, levanta el fence (se quita la concurrencia reservada; se rechaza
para un clúster separado sin la frase escrita), ejecuta `30-switch-traffic.sh --to primary` (pesos 255 / 0) e imprime el
estado del fence de ambas regiones (ninguna debería tener fence). Última línea esperada:
`restored. Run: scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe`.

**5c. Validar**

```bash
bash scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe
bash scripts/dr/45-consistency.sh
```

Esperado `validation passed` y `consistent: true`, dashboard en `us-east-2` / `writer`, y los conteos incluyen todo lo escrito durante el
simulacro. Con los workflows, los pasos 5a a 5c son una sola ejecución de `failover` con `direction` = `to-primary`, que **primero**
levanta el fence del primario (su base de datos es entonces reader y atiende solo lecturas mientras el tráfico sigue en el secundario),
luego cambia la base de datos y los pesos, y valida. Deshacer 5b: `bash scripts/dr/10-simulate-outage.sh` (fence otra vez) y `bash scripts/dr/30-switch-traffic.sh --to secondary`.

### Paso 6. Revisión del estado final

`bash scripts/dr/00-precheck.sh` muestra el estado normal: `us-east-2` writer, `us-west-2` reader, lag menor de 5000 ms,
sin fence, pesos 255 / 0. Las alarmas de CloudWatch vuelven a OK. Si terminaste, destruye el demo
([cost-and-cleanup.md](../docs/cost-and-cleanup.md)): cuesta unos 13 USD al día mientras corre (estimación no verificada).

## Camino no planificado: una caída regional real

Úsalo solo cuando `us-east-2` esté realmente no disponible y aceptes perder las últimas escrituras. Los scripts son los
mismos; las diferencias son el fence, las confirmaciones escritas y que el primario antiguo no se puede revertir.

**Lo que aceptas.** La replicación es asíncrona (RPO de aproximadamente un segundo típicamente, no cero).
`failover-global-cluster --allow-data-loss` promueve al secundario en el punto al que hubiera llegado la replicación y
**separa** al primario antiguo del clúster global. El primario antiguo, cuando su región vuelva, es un clúster
independiente que **todavía puede aceptar escrituras** (split brain). Por eso el orden es siempre: **fence de la región
antigua, promover la base de datos y después cambiar el DNS.**

### U1. Registrar lo que se sabe

```bash
bash scripts/dr/00-precheck.sh
```

Prueba ambas regiones para encontrar el clúster global, así que funciona con una región caída. Reportará problemas de
`us-east-2` (salida 1); es lo esperado. Conserva la última línea base (de la verificación previa a la caída, si la tienes) y
abre CloudWatch en `us-west-2`: la métrica `AuroraGlobalDBReplicationLag` de `rdsc-uswest2-drdemo-prod` antes de la caída
indica cuántos milisegundos de escrituras podría perder el failover. Deshacer: nada.

### U2. Fence de la región antigua y failover

```bash
bash scripts/dr/20-promote.sh --unplanned
```

Qué hace, en orden: imprime la lectura de RPO; intenta hacer fence de `us-east-2` (concurrencia reservada 0). Si la API de
Lambda de `us-east-2` no responde, avisa y te pide escribir `proceed-unfenced us-east-2` (o definir
`PROMOTE_UNFENCED_CONFIRM` con esa frase); luego te pide escribir `allow-data-loss us-west-2`; después llama a
`failover-global-cluster --allow-data-loss` mediante la API de RDS de `us-west-2` y sondea hasta que `us-west-2` sea el
writer. Si la llamada a la API falla, reintenta con `--api-region us-east-2` solo si esa región responde.

**Resultado esperado.** `failover finished: us-west-2 is the writer. KEEP us-east-2 FENCED until its cluster is deleted or
rebuilt as a secondary.`

**Cómo verificar.** La lista de miembros que imprime el script, o `aws rds describe-global-clusters --region us-west-2`.

**Cómo deshacer.** No hay forma de deshacerlo. El primario antiguo queda separado. El camino de vuelta es U7 y U8.

Si no pudiste hacer fence de `us-east-2` durante la caída, hazlo **en cuanto la región responda de nuevo**, antes que
cualquier otra cosa, con `bash scripts/dr/10-simulate-outage.sh` (el mismo mecanismo; el nombre dice "simulate", el efecto
es el fence).

### U3. Cambiar Route 53

```bash
bash scripts/dr/30-switch-traffic.sh
```

El script comprueba que el clúster de `us-west-2` es el writer, detecta que el clúster de `us-east-2` no es reader del
clúster global (está separado o inalcanzable), verifica que sus funciones tienen fence, les hace fence ahora si no, y pide
`proceed-unfenced us-east-2` solo si eso es imposible. Luego actualiza el stack dns en `us-east-1`, lo cual funciona con
`us-east-2` caída. Esperado: pesos 0 / 255 y `done. New requests reach us-west-2 within about 60 s ...`.

### U4. Validar

```bash
bash scripts/dr/40-validate.sh --expect-region us-west-2 --wait 180 --probe
```

Más el dashboard y la carpeta Postman `04 DR drill`, como en el paso 4. Esperado `validation passed`, `database_role`
`writer`.

### U5. Medir la brecha de datos

La base de datos superviviente no puede decirte por sí sola lo que nunca recibió. Compárala con un registro independiente:

1. **Contra la línea base.** En la región promovida, lee `/stats` justo después del failover (`40-validate.sh` lo imprime)
   y compara `counts_by_region` y `last_processed_at_by_region` de `us-east-2` con la última línea base de
   `00-precheck.sh` y con lo que mostraba el dashboard antes de la caída. La diferencia en el conteo, más el tiempo entre
   `last_processed_at_by_region["us-east-2"]` y la hora de la caída, es la brecha visible. Hazlo antes de que nuevas
   escrituras confundan la comparación (las escrituras de la región nueva cuentan bajo `us-west-2`, así que la cifra de
   `us-east-2` sigue siendo comparable).
2. **Contra los clientes.** Cualquier cliente que tenga un `id` de una respuesta `201` puede comprobarlo con
   `GET /transactions?limit=100`; la app bancaria lo hace con las operaciones de su sesión mediante **Verify my operations** (lista los
   ids confirmados que faltan en el ledger). Los clientes que no recibieron respuesta, o cuya respuesta no aparece en los datos
   supervivientes, pueden reenviar con la **misma `Idempotency-Key`**: si la fila se replicó reciben `200` con el registro
   original y no se duplica nada; si no se replicó, se crea en el nuevo primario (con la región y la hora nuevas).
3. **Contra el clúster antiguo.** Cuando `us-east-2` vuelva, toma un snapshot del clúster antiguo independiente antes de
   tocarlo (`aws rds create-db-cluster-snapshot --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod
   --db-cluster-snapshot-identifier drdemo-before-rebuild`), restáuralo en un clúster temporal y compara los ids de las
   transacciones con el nuevo primario. Las filas que falten pueden reinsertarse mediante la API. Es trabajo manual y nunca
   se ha ejecutado.
4. **Cota.** `AuroraGlobalDBReplicationLag` (y `AuroraGlobalDBRPOLag`) antes de la caída acota la ventana de tiempo de las escrituras
   perdidas. `scripts/lib/dr.sh` indica que `AuroraGlobalDBRPOLag` solo se llena cuando `rds.global_db_rpo` está configurado, y las
   plantillas de este repositorio no lo definen. Aun así, el simulacro del 2026-11-11 leyó 14 datapoints de esa métrica (máximo
   1531 ms). Por qué se llena no se verificó desde el repositorio: comprueba la métrica en CloudWatch antes de apoyarte en ella y trata
   `AuroraGlobalDBReplicationLag` como la cota principal.

Anota el resultado. Exprésalo como "unas N transacciones no se replicaron", no como "cero pérdida".

### U6. Mantener el fence en us-east-2

Hasta que el clúster antiguo se borre o se reconstruya como secundario, sus funciones se quedan en concurrencia reservada
0. Verifica con `bash scripts/dr/00-precheck.sh` (el paso 4 lista la concurrencia reservada de ambas regiones). Si la
región era inalcanzable cuando promoviste, ejecuta `bash scripts/dr/10-simulate-outage.sh` en cuanto responda.

### Por qué el secreto maestro se pierde con la región

La contraseña maestra de Aurora es un secreto de Secrets Manager **solo en la región primaria** (`us-east-2`,
`sm-useast2-drdemo-master-prod-<sufijo>`, generado por el stack de base de datos porque RDS no puede administrar la contraseña
maestra de una base de datos global). Un clúster secundario no tiene secreto propio. Cuando se pierde `us-east-2`, el secreto se
pierde o es inalcanzable con ella, y el clúster promovido en `us-west-2` no tiene un secreto maestro en su propia región. Consecuencias:

- La aplicación **no** se ve afectada: las funciones usan autenticación IAM de base de datos (un token firmado por el rol
  de la función). No interviene ninguna contraseña.
- Los únicos usuarios de las credenciales maestras son el bootstrap único del esquema (ya hecho y replicado) y la
  administración manual. `scripts/deploy.sh` omite el paso del esquema cuando los roles están intercambiados.
- Si necesitas acceso maestro en `us-west-2`, define una contraseña maestra nueva en el clúster promovido (por ejemplo
  `aws rds modify-db-cluster --region us-west-2 --db-cluster-identifier rdsc-uswest2-drdemo-prod --master-user-password
  '<nueva contraseña>' --apply-immediately`). No se verificó que RDS lo acepte en un antiguo secundario, y los secretos maestros
  administrados por RDS no están disponibles para bases de datos globales; consulta antes la documentación de RDS. No pongas la contraseña en el repositorio.

### Alternativa: separar (detach) en lugar de failover

Si no se puede usar `failover-global-cluster`, el secundario puede separarse y queda como clúster independiente y escribible:
`aws rds remove-from-global-cluster --region us-west-2 --global-cluster-identifier <id del clúster global>
--db-cluster-identifier <arn del clúster de us-west-2>`. También es irreversible, pierde las mismas escrituras y deja un
clúster global que aún lista al primario muerto; `scripts/dr/*` esperan pertenencia al clúster global, así que casi todos
los scripts se detendrán con un mensaje. Este camino no está automatizado y nunca se ha ejecutado. Prefiere U2.

### U7. Reconstrucción manual de un secundario en us-east-2 (para el failback)

Los scripts no pueden hacer failback a un clúster que no es miembro del clúster global (`20-promote.sh` y
`50-failback-promote.sh` se detienen con un mensaje). Para volver a `us-east-2` primero debes crear allí un secundario
nuevo que replique desde `us-west-2`. **Este procedimiento es manual, se escribió a partir de la documentación de AWS CLI y
nunca se ha ejecutado.** Crea un clúster que se factura (unos 6,40 USD al día, estimación no verificada). Hazlo con
credenciales de propietario, en `us-east-2`, cuando la región esté sana y mientras sus funciones sigan con fence.

1. **Conservar evidencia** (sección U5, punto 3): haz snapshot del clúster antiguo independiente y concilia los datos antes.
2. **Liberar los identificadores.** Los scripts y el procedimiento de destrucción buscan `rdsc-useast2-drdemo-prod` y
   `rds-useast2-drdemo-prod`, así que el clúster reconstruido debe reutilizar esos nombres. Es destructivo; confirma que
   tienes el snapshot antes.

   ```bash
   aws rds modify-db-cluster --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod \
     --no-deletion-protection --apply-immediately
   aws rds delete-db-instance --region us-east-2 --db-instance-identifier rds-useast2-drdemo-prod
   aws rds delete-db-cluster --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod --skip-final-snapshot
   ```

   (Espera a que terminen ambos borrados. Si la región perdió sus datos, omite este paso.)
3. **Crear el clúster nuevo como miembro del mismo clúster global.** Reutiliza los objetos que el stack de base de datos
   todavía posee en `us-east-2` (clave KMS, grupo de subredes, grupo de seguridad, grupo de parámetros). Lee antes los valores:

   ```bash
   aws cloudformation describe-stacks --region us-east-2 --stack-name stk-useast2-drdemo-database-prod \
     --query "Stacks[0].Outputs[?OutputKey=='KmsKeyArn'].OutputValue" --output text
   aws cloudformation describe-stack-resource --region us-east-2 --stack-name stk-useast2-drdemo-database-prod \
     --logical-resource-id DbSecurityGroup --query StackResourceDetail.PhysicalResourceId --output text
   ```

   Luego crea el clúster y su instancia (la versión del motor debe coincidir con la que está en uso; `16.15` es la
   predeterminada; las etiquetas son las cinco obligatorias; sin usuario ni contraseña maestros, porque un secundario los hereda):

   ```bash
   aws rds create-db-cluster --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod \
     --engine aurora-postgresql --engine-version 16.15 \
     --global-cluster-identifier rdsg-useast2-drdemo-prod --source-region us-west-2 \
     --storage-encrypted --kms-key-id <KmsKeyArn of the us-east-2 database stack> \
     --db-subnet-group-name dbsg-useast2-drdemo-prod --vpc-security-group-ids <DbSecurityGroup id> \
     --db-cluster-parameter-group-name dbpg-useast2-drdemo-cluster-prod \
     --enable-iam-database-authentication --backup-retention-period 1 --copy-tags-to-snapshot \
     --enable-cloudwatch-logs-exports postgresql --deletion-protection \
     --tags Key=team-owner,Value=nube-segura Key=project-name,Value=aws-multi-region-aurora-failover-active-passive Key=app-name,Value=dr-demo \
            Key=repo-name,Value=aws-multi-region-aurora-failover-active-passive Key=env-type,Value=prod
   aws rds create-db-instance --region us-east-2 --db-instance-identifier rds-useast2-drdemo-prod \
     --db-cluster-identifier rdsc-useast2-drdemo-prod --db-instance-class db.r6g.large --engine aurora-postgresql \
     --db-subnet-group-name dbsg-useast2-drdemo-prod --no-publicly-accessible --no-enable-performance-insights \
     --tags Key=team-owner,Value=nube-segura Key=project-name,Value=aws-multi-region-aurora-failover-active-passive Key=app-name,Value=dr-demo \
            Key=repo-name,Value=aws-multi-region-aurora-failover-active-passive Key=env-type,Value=prod
   ```

   Si el identificador del clúster global no es `rdsg-useast2-drdemo-prod`, usa el que imprimió U2 (`aws rds
   describe-global-clusters --region us-west-2`).
4. **Esperar la copia inicial.** `aws rds describe-global-clusters --region us-west-2 --global-cluster-identifier
   <id del clúster global> --query 'GlobalClusters[0].GlobalClusterMembers'` debe listar ambos clústeres, el nuevo con
   `IsWriter` en false. Entonces `bash scripts/dr/00-precheck.sh` debería mostrar `us-east-2 ... reader` y un retraso de
   replicación menor de 5000 ms. El tiempo de la copia depende de la cantidad de datos; no se midió.
5. **Apuntar las funciones de us-east-2 al clúster nuevo.** La autenticación IAM de base de datos se concede por **id de
   recurso del clúster**, y el clúster nuevo tiene uno nuevo, así que el permiso `rds-db:connect` de los seis roles de
   función ya no coincide. Lee el id nuevo y actualiza el stack api de `us-east-2` conservando el resto de parámetros:

   ```bash
   aws rds describe-db-clusters --region us-east-2 --db-cluster-identifier rdsc-useast2-drdemo-prod \
     --query 'DBClusters[0].[DbClusterResourceId,Endpoint]' --output text
   aws cloudformation update-stack --region us-east-2 --stack-name stk-useast2-drdemo-api-prod --use-previous-template \
     --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
     --parameters ParameterKey=DbClusterResourceId,ParameterValue=<new resource id> \
       ParameterKey=EnvType,UsePreviousValue=true ParameterKey=ApiDomainName,UsePreviousValue=true \
       ParameterKey=HostedZoneId,UsePreviousValue=true ParameterKey=CognitoUserPoolArn,UsePreviousValue=true \
       ParameterKey=CognitoBankClientId,UsePreviousValue=true ParameterKey=CognitoDashboardClientId,UsePreviousValue=true \
       ParameterKey=AllowedOrigins,UsePreviousValue=true ParameterKey=DbEndpoint,UsePreviousValue=true \
       ParameterKey=DbName,UsePreviousValue=true ParameterKey=DbWriterUser,UsePreviousValue=true \
       ParameterKey=DbReaderUser,UsePreviousValue=true ParameterKey=PrivateSubnetIds,UsePreviousValue=true \
       ParameterKey=LambdaSecurityGroupId,UsePreviousValue=true ParameterKey=KmsKeyArn,UsePreviousValue=true \
       ParameterKey=AlertTopicArn,UsePreviousValue=true ParameterKey=LogLevel,UsePreviousValue=true \
       ParameterKey=LogRetentionDays,UsePreviousValue=true ParameterKey=ThrottleRate,UsePreviousValue=true \
       ParameterKey=ThrottleBurst,UsePreviousValue=true \
       ParameterKey=PermissionsBoundaryArn,UsePreviousValue=true
   ```

   El endpoint impreso debería ser igual al `DbEndpoint` del stack (se deriva del identificador del clúster); si difiere,
   pasa también el endpoint nuevo. No uses `scripts/deploy.sh --steps api` para esto: lee las salidas (obsoletas) del stack
   de base de datos. Después comprueba con `bash scripts/dr/00-precheck.sh` que las funciones siguen con fence (paso 4).
6. **Drift.** El stack de base de datos de `us-east-2` ya no coincide con la realidad y no debe actualizarse nunca más.
   Como el clúster nuevo reutiliza los mismos identificadores, `scripts/destroy.sh` lo encuentra (el paso 1 le quita la
   protección contra borrado, el paso 4 lo separa) y CloudFormation probablemente lo borrará junto con el stack; no se
   verificó que lo haga, así que revisa la lista de sobrantes de la destrucción ([cost-and-cleanup.md](../docs/cost-and-cleanup.md)).

### U8. Failback

Cuando el nuevo secundario esté `available` y sincronizado (lag bajo), se aplica el failback normal:

```bash
bash scripts/dr/50-failback-promote.sh
bash scripts/dr/60-restore.sh
bash scripts/dr/40-validate.sh --expect-region us-east-2 --wait 180 --probe
```

Mismos resultados esperados, verificación y cómo deshacer que los pasos 5a a 5c. `60-restore.sh` levanta el fence solo
porque el clúster vuelve a ser miembro del clúster global; en caso contrario exige la frase escrita
`unfence-standalone us-east-2`. También puedes **no** hacer failback: `us-west-2` puede seguir como writer el resto del
demo (los pesos serán entonces 0 / 255) y destruyes todo al final.

## Limitaciones conocidas

- `20-promote.sh --unplanned` (y el modo no planificado de `50-failback-promote.sh`) necesita una terminal interactiva para
  la frase `allow-data-loss <región>`; no se puede dar por adelantado.
- `10-simulate-outage.sh` es la simulación del simulacro **y** el fence que se usa tras un failover no planificado (el mismo
  mecanismo, concurrencia reservada 0). El nombre dice "simulate"; en U2 y U6 su efecto es el fence.
- Desde una estación de trabajo, actualizar el stack dns (`30-switch-traffic.sh`, `60-restore.sh`) requiere tus credenciales de
  propietario. El workflow `failover` lo hace con el rol de la organización. `CFN_EXEC_ROLE_ARN` solo elige el rol que CloudFormation
  usa para el cambio.
- Los workflows ejecutan solo un switchover planificado. Una caída real (no planificada) es siempre un paso manual de este runbook.
- El grupo de concurrencia compartido `drdemo-prod-infrastructure` puede reemplazar en silencio una ejecución en cola por otra pendiente más nueva.
- Reenviar una `Idempotency-Key` con un cuerpo distinto devuelve el registro original (`200`); la API no informa conflicto.
  Tenlo presente al reenviar tras una brecha de datos (U5, punto 2).
- Los scripts asumen el entorno `prod`.
- El camino no planificado, U5 y U7 nunca se ejecutaron en AWS. El camino planificado se ejecutó a mano antes de Cognito (2026-10-14) y una vez con el runner automatizado después (2026-11-11); ver [Valores observados](#valores-observados).
- El workflow `failover` no puede probar el ledger por sí solo: no tiene token de Cognito, así que el depósito de prueba y
  `45-consistency.sh` se omiten ahí. Ejecuta `45-consistency.sh` con `COGNITO_ID_TOKEN` después de cada movimiento.
- Un ID token dura 15 minutos y un refresh token 24 horas; los sitios se refrescan y se reautentican solos contra la región que
  responde, los scripts y Postman no.

## Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `00-precheck.sh` sale con 1, lag superior a 5000 ms | La replicación va retrasada | Espera y repite; no hagas un switchover planificado hasta que baje |
| `20-promote.sh`: `lag ... is above ... ms` | Lo mismo | Espera, o `--force` (el propio switchover igual espera a que el secundario se ponga al día) |
| `20-promote.sh`: `switchover request failed (nothing else was changed)` | API de RDS de la región del writer antiguo inalcanzable o falta un permiso | Reintenta con `--api-region us-west-2` |
| `20-promote.sh`: `the cluster in us-west-2 is not a member of ...` | Un failover no planificado anterior lo separó, o el clúster global ya no existe | Sigue U7 |
| `30-switch-traffic.sh`: `... is reader, not the writer. Promote it first` | El tráfico debe seguir a la promoción | Ejecuta antes el paso 2 (o 5a) |
| `40-validate.sh`: `expected region ... was not reached` | Cachés de DNS y keep-alive | Espera, recarga la pestaña del dashboard, revisa los pesos con `00-precheck.sh` |
| `503 database_read_only` tras el cambio | El DNS se movió antes de promover la base de datos, o la promoción no ha terminado | Revisa los roles con `00-precheck.sh`; termina la promoción; `Retry-After` es 5 s |
| `503 database_unavailable` justo después de una promoción | Se están reemplazando las conexiones con el writer antiguo, o falló la autenticación o venció un statement | Repite la solicitud tras unos segundos; si persiste, revisa los roles con `00-precheck.sh` y los logs de las funciones |
| `500 internal_error` | Un error de base de datos que no es de conectividad ni de rol (esquema, datos o un bug) | Lee el grupo de logs de la función (campos `sqlstate` y `kind`; no se registran cuerpos) |
| `422 insufficient_funds` | Un retiro mayor que el saldo | Esperado; deposita primero. La respuesta trae el saldo actual |
| `503 database_busy` | No se obtuvo el bloqueo de la fila de la cuenta en 5 s | Reintenta tras unos segundos (la app bancaria reintenta sola con la misma clave) |
| `429` | Se superó el límite de throttling del stage (20 solicitudes por segundo, ráfaga 40) | Espera a que baje la tasa; cierra pestañas extra del dashboard (cada una hace tres solicitudes cada 5 segundos) |
| `401` (o `403`) en todas las solicitudes protegidas | Token ausente, vencido (15 minutos) o incorrecto: sin cabecera `Authorization: Bearer <ID token>`, un access token en lugar del ID token, o un token de otro pool o cliente | Inicia sesión de nuevo en la app bancaria o el dashboard; para Postman y los scripts obtén un ID token nuevo y define `cognito_id_token` / `COGNITO_ID_TOKEN`. `GET /health` y `OPTIONS` no necesitan token |
| `401` de una región justo después de un switchover | La réplica de Cognito no está activa en us-west-2 (camino con scripts: no se ejecutó la activación), o el token no lo acepta el authorizer regional | Comprueba que la ejecución de `failover` activó la réplica (paso "Activate Cognito replica") o ejecuta el helper de arriba; inicia sesión de nuevo para que la página se reautentique contra la región que responde |
| Una ejecución de `failover` no arranca o es reemplazada | Otro workflow del grupo de concurrencia compartido corre o está pendiente | Abre la lista de ejecuciones, espera al job en curso y lanza de nuevo |
| `failover` to-secondary se detiene en "Primary region not fenced" | Las funciones del primario no están en concurrencia 0 | Ejecuta antes `simulate-outage` con `fence` |
| `20-promote.sh`: `no replication lag datapoint ... after waiting` | El secundario nuevo aún no publicó la métrica, o la consulta de la métrica falló | Ejecuta `00-precheck.sh`, espera unos minutos y repite el paso; no uses `--force` salvo que aceptes el riesgo |
| `--undo` o `60-restore.sh` pide `unfence-standalone ...` | El clúster no es miembro del clúster global | No levantes el fence hasta terminar U7 |
| El dashboard sigue sin disponibilidad tras el paso 3 | Caché de resolvers o de conexiones, pestaña antigua | Recarga forzada; espera hasta unos minutos; ejecuta `40-validate.sh --wait 180` |
| `ROLLBACK_COMPLETE` u otro stack atascado | Un despliegue anterior falló | Investiga con `aws cloudformation describe-stack-events`; los scripts nunca borran ni reintentan un stack atascado |
