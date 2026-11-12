# Informe de la prueba de DR

Versión en inglés (fuente): [docs/dr-test-report.md](../docs/dr-test-report.md). Esta es una copia en español; si difieren, manda el inglés.

Fecha de la prueba (UTC): 2026-11-11.

Generado con `python -m dr_drill formal-report` a partir del `timeline.json` de 1 carpeta(s) de ejecución: `2026-11-11T12-00-00Z`. Todos los números salen de esas líneas de tiempo; ninguno se escribe a mano.

## Propósito

Medir, con un script repetible, cuánto tarda la demo en volver a atender desde la segunda región en cada sentido (RTO), si se pierde alguna escritura confirmada (RPO del switchover planificado) y en qué se va el tiempo. La meta de RTO de 15 minutos del proyecto es una meta, no un resultado de este informe.

## Método

Un runner en una estación de trabajo maneja el entorno prod real mediante la API pública y los workflows de GitHub. Fases, en orden: `baseline` (carga estable sobre la región primaria), `fence` (el workflow `simulate-outage` limita a cero las funciones de la región primaria), `to-secondary` (el workflow `failover` promueve la región secundaria), `steady-secondary`, `to-primary` (el workflow `failover` vuelve a la primaria), `steady-primary` y `consistency` (verificación final del libro mayor). Cuatro sondas corren durante todo el simulacro: una de escritura (un intento cada 1 s, depósitos y retiros de 0.01 alternados, de modo que el libro queda neto en cero; un movimiento sin confirmar se reintenta con la misma clave de idempotencia), una de lectura (`GET /account` cada 5 s), una de salud (`GET /health?deep=true` cada 5 s) y una de inicio de sesión contra la región secundaria (cada 10 s). Un observador de DNS consulta el nombre de la API cada 2 s desde la estación de trabajo. El retraso de replicación se lee de CloudWatch al terminar, después de que el runner espera 120 s por los últimos datos. Las horas son UTC. Los percentiles son de rango más cercano. El simulacro lanza los workflows con un script; el cambio en sí es el cambio manual de enrutamiento ponderado del proyecto, ejecutado por el workflow.

## Entorno

- Regiones: primaria `us-east-2` (escritora al inicio), secundaria `us-west-2`.
- Base de datos: Aurora PostgreSQL Global Database, clase de instancia `db.x1.sample`, versión del motor `9.9` (leídas de `docs/technical.md`).
- Tráfico: registros ponderados de Route 53 que cambia el workflow `failover`; las sondas llaman al nombre público de la API, así que recorren el mismo camino que un usuario.
- Este informe no incluye datos de cuenta.

## Resultados principales

| Ejecución | Fecha (UTC) | Veredicto | RTO de escritura to-secondary | RTO de escritura to-primary | RPO medido |
|---|---|---|---|---|---|
| 1 | 2026-11-11 | PASS | 60.00 s | 10.00 s | 0 escrituras confirmadas perdidas |

## Métricas

Una métrica sin datos se informa como no medida, nunca como cero.

### Ejecución 1: 2026-11-11T12-00-00Z

| Métrica | Valor | Definición | Fuente | Resolución |
|---|---|---|---|---|
| to-secondary: RTO de escritura | 60.00 s | Última escritura 2xx respondida por us-east-2 antes de la primera escritura fallida o reintentada, hasta la primera escritura 2xx respondida por us-west-2. | sonda de escritura, un intento cada 1 s | cerca de 1 s |
| to-secondary: RTO de lectura | 70.00 s | Lo mismo con `GET /account`: última respuesta 2xx de us-east-2 antes de la primera lectura fallida, hasta la primera 2xx de us-west-2. | sonda de lectura, `GET /account` cada 5 s | cerca de 5 s |
| to-secondary: indisponibilidad de escritura | 58.05 s | Primera escritura fallida hasta la primera escritura 2xx respondida por us-west-2. | sonda de escritura, un intento cada 1 s | cerca de 1 s |
| to-secondary: disponibilidad del inicio de sesión en la secundaria | 66.7 % | Proporción de inicios de sesión exitosos en la región secundaria desde el inicio de la caída hasta el fin de la fase estable (6 intentos, 2 fallidos). | sonda de inicio de sesión, uno cada 10 s | 10 s |
| to-secondary: racha más larga de inicios de sesión fallidos | 2 inicios de sesión, 20.04 s | Racha más larga de inicios de sesión fallidos consecutivos y su duración. | sonda de inicio de sesión, uno cada 10 s | 10 s |
| to-secondary: convergencia de DNS | 1.00 s | Fin del paso de cambio de tráfico hasta la primera respuesta DNS distinta (negativa si la respuesta cambió antes de que terminara el paso). | observador de DNS y los tiempos de los pasos de GitHub | 2 s, un resolvedor |
| to-secondary: tiempo de decisión | 3.00 s | Fin de la ejecución de fence hasta el lanzamiento del failover. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| to-primary: RTO de escritura | 10.00 s | Última escritura 2xx respondida por us-west-2 antes de la primera escritura fallida o reintentada, hasta la primera escritura 2xx respondida por us-east-2. | sonda de escritura, un intento cada 1 s | cerca de 1 s |
| to-primary: RTO de lectura | no medido: no se vio ninguna solicitud fallida o reintentada en la ventana (no se observó caída) | Lo mismo con `GET /account`: última respuesta 2xx de us-west-2 antes de la primera lectura fallida, hasta la primera 2xx de us-east-2. | sonda de lectura, `GET /account` cada 5 s | cerca de 5 s |
| to-primary: indisponibilidad de escritura | 8.05 s | Primera escritura fallida hasta la primera escritura 2xx respondida por us-east-2. | sonda de escritura, un intento cada 1 s | cerca de 1 s |
| to-primary: disponibilidad del inicio de sesión en la secundaria | 100.0 % | Proporción de inicios de sesión exitosos en la región secundaria desde el inicio de la caída hasta el fin de la fase estable (3 intentos, 0 fallidos). | sonda de inicio de sesión, uno cada 10 s | 10 s |
| to-primary: racha más larga de inicios de sesión fallidos | 0 inicios de sesión, 0.00 s | Racha más larga de inicios de sesión fallidos consecutivos y su duración. | sonda de inicio de sesión, uno cada 10 s | 10 s |
| to-primary: convergencia de DNS | 1.00 s | Fin del paso de cambio de tráfico hasta la primera respuesta DNS distinta (negativa si la respuesta cambió antes de que terminara el paso). | observador de DNS y los tiempos de los pasos de GitHub | 2 s, un resolvedor |
| to-primary: tiempo de decisión | no medido: no aplica: el regreso se lanza tras una fase estable de duración fija | Fin de la ejecución de fence hasta el lanzamiento del failover. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| simulate-outage (fence): tiempo en cola | 3.50 s | Lanzamiento hasta el inicio de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| simulate-outage (fence): tiempo de ejecución | 16.00 s | Inicio de la ejecución hasta su última actualización. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| simulate-outage (fence): total | 19.50 s | Lanzamiento hasta el fin de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-secondary): tiempo en cola | 4.00 s | Lanzamiento hasta el inicio de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-secondary): tiempo de ejecución | 54.00 s | Inicio de la ejecución hasta su última actualización. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-secondary): total | 58.00 s | Lanzamiento hasta el fin de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-primary): tiempo en cola | 4.00 s | Lanzamiento hasta el inicio de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-primary): tiempo de ejecución | 33.00 s | Inicio de la ejecución hasta su última actualización. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-primary): total | 37.00 s | Lanzamiento hasta el fin de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| AuroraGlobalDBReplicationLag: máximo | 1300.0 ms | Máximo de las 4 muestras tomadas durante la ejecución (milisegundos). | CloudWatch AuroraGlobalDBReplicationLag, máximo por período | período de 60 s |
| AuroraGlobalDBReplicationLag: percentil 95 | 1300.0 ms | Percentil 95 de las 4 muestras tomadas durante la ejecución (milisegundos). | CloudWatch AuroraGlobalDBReplicationLag, máximo por período | período de 60 s |
| AuroraGlobalDBReplicationLag: promedio | 1042.5 ms | Promedio de las 4 muestras tomadas durante la ejecución (milisegundos). | CloudWatch AuroraGlobalDBReplicationLag, máximo por período | período de 60 s |

## Línea de tiempo

Los desfases son segundos desde el inicio de cada sentido (el lanzamiento de la ejecución `fence` para `to-secondary`; el inicio de la fase `to-primary` para `to-primary`). Se omiten los pasos de workflow más cortos que la resolución de las marcas de tiempo de GitHub.

### Fases de la ejecución 1

| Fase | Inicio (UTC) | Fin (UTC) | Duración |
|---|---|---|---|
| baseline | 2026-11-11T12:00:00.000Z | 2026-11-11T12:00:20.000Z | 20.0 s |
| fence | 2026-11-11T12:00:20.000Z | 2026-11-11T12:00:42.000Z | 22.0 s |
| to-secondary | 2026-11-11T12:00:42.000Z | 2026-11-11T12:01:42.000Z | 60.0 s |
| steady-secondary | 2026-11-11T12:01:42.000Z | 2026-11-11T12:02:02.000Z | 20.0 s |
| to-primary | 2026-11-11T12:02:02.000Z | 2026-11-11T12:02:41.000Z | 39.0 s |
| steady-primary | 2026-11-11T12:02:41.000Z | 2026-11-11T12:03:01.000Z | 20.0 s |
| consistency | 2026-11-11T12:03:01.000Z | 2026-11-11T12:03:02.000Z | 1.0 s |

### Ejecución 1: to-secondary (fence y luego failover a la región secundaria)

| Desfase | Hora (UTC) | Evento |
|---|---|---|
| +0.0 s | 2026-11-11T12:00:20.000Z | Empieza la fase `fence` |
| +0.5 s | 2026-11-11T12:00:20.500Z | Lanzamiento de `simulate-outage` (fence) |
| +4.0 s | 2026-11-11T12:00:24.000Z | `simulate-outage` (fence) empieza en el runner |
| +10.0 s | 2026-11-11T12:00:30.000Z | Paso Run 10-simulate-outage.sh (bloqueo (fence), 8 s) |
| +14.0 s | 2026-11-11T12:00:34.000Z | Primer intento de escritura fallido |
| +20.0 s | 2026-11-11T12:00:40.000Z | Termina `simulate-outage` (fence) |
| +23.0 s | 2026-11-11T12:00:43.000Z | Lanzamiento de `failover` (to-secondary) |
| +27.0 s | 2026-11-11T12:00:47.000Z | `failover` (to-secondary) empieza en el runner |
| +34.0 s | 2026-11-11T12:00:54.000Z | Paso Group 1 - pre-checks (read only) (verificaciones previas, 4 s) |
| +38.0 s | 2026-11-11T12:00:58.000Z | Paso Group 1 - confirm that the primary region is fenced (verificaciones previas, 2 s) |
| +40.0 s | 2026-11-11T12:01:00.000Z | Paso Group 1 - confirm that the secondary region is NOT fenced (verificaciones previas, 2 s) |
| +42.0 s | 2026-11-11T12:01:02.000Z | Paso Group 1 - unfence the primary region (bloqueo (fence), 3 s) |
| +48.0 s | 2026-11-11T12:01:08.000Z | Paso Group 2 - activate the Cognito replica in the secondary region (réplica de Cognito, 5 s) |
| +53.0 s | 2026-11-11T12:01:13.000Z | Paso Group 2 - planned switchover to the secondary region (switchover, 12 s) |
| +68.0 s | 2026-11-11T12:01:28.000Z | Paso Group 3 - switch the Route 53 weights (cambio de tráfico, 3 s) |
| +71.0 s | 2026-11-11T12:01:31.000Z | Paso Group 3 - validate the service on the custom domain (validación, 6 s) |
| +72.0 s | 2026-11-11T12:01:32.000Z | Primera respuesta DNS distinta tras empezar el cambio de tráfico (un resolvedor) |
| +72.0 s | 2026-11-11T12:01:32.045Z | Primera escritura respondida por us-west-2 |
| +77.0 s | 2026-11-11T12:01:37.000Z | Paso Group 3 - consistency check of the ledger (verificación de consistencia, 3 s) |
| +81.0 s | 2026-11-11T12:01:41.000Z | Termina `failover` (to-secondary) |
| +82.0 s | 2026-11-11T12:01:42.000Z | Empieza la fase estable `steady-secondary` |

### Ejecución 1: to-primary (regreso a la región primaria)

| Desfase | Hora (UTC) | Evento |
|---|---|---|
| +0.0 s | 2026-11-11T12:02:02.000Z | Empieza la fase `to-primary` |
| +1.0 s | 2026-11-11T12:02:03.000Z | Lanzamiento de `failover` (to-primary) |
| +5.0 s | 2026-11-11T12:02:07.000Z | `failover` (to-primary) empieza en el runner |
| +12.0 s | 2026-11-11T12:02:14.000Z | Paso Group 1 - pre-checks (read only) (verificaciones previas, 4 s) |
| +19.0 s | 2026-11-11T12:02:21.000Z | Paso Group 2 - planned switchover back to the primary region (switchover, 7 s) |
| +24.0 s | 2026-11-11T12:02:26.000Z | Primer intento de escritura fallido |
| +26.0 s | 2026-11-11T12:02:28.000Z | Paso Group 2 - deactivate the Cognito replica in the secondary region (réplica de Cognito, 2 s) |
| +30.0 s | 2026-11-11T12:02:32.000Z | Paso Group 3 - switch the Route 53 weights (cambio de tráfico, 2 s) |
| +32.0 s | 2026-11-11T12:02:34.000Z | Paso Group 3 - validate the service on the custom domain (validación, 4 s) |
| +32.1 s | 2026-11-11T12:02:34.055Z | Primera escritura respondida por us-east-2 |
| +33.0 s | 2026-11-11T12:02:35.000Z | Primera respuesta DNS distinta tras empezar el cambio de tráfico (un resolvedor) |
| +36.0 s | 2026-11-11T12:02:38.000Z | Paso Group 3 - consistency check of the ledger (verificación de consistencia, 1 s) |
| +38.0 s | 2026-11-11T12:02:40.000Z | Termina `failover` (to-primary) |
| +39.0 s | 2026-11-11T12:02:41.000Z | Empieza la fase estable `steady-primary` |

### Ejecución 1: tiempo por categoría de paso del workflow

| Sentido | Categoría | Duración |
|---|---|---|
| to-secondary | cola | 7.50 s |
| to-secondary | preparación | 19.00 s |
| to-secondary | verificaciones previas | 8.00 s |
| to-secondary | bloqueo (fence) | 11.00 s |
| to-secondary | réplica de Cognito | 5.00 s |
| to-secondary | switchover | 12.00 s |
| to-secondary | cambio de tráfico | 3.00 s |
| to-secondary | validación | 6.00 s |
| to-secondary | verificación de consistencia | 3.00 s |
| to-primary | cola | 4.00 s |
| to-primary | preparación | 12.00 s |
| to-primary | verificaciones previas | 4.00 s |
| to-primary | réplica de Cognito | 2.00 s |
| to-primary | switchover | 7.00 s |
| to-primary | cambio de tráfico | 2.00 s |
| to-primary | validación | 4.00 s |
| to-primary | verificación de consistencia | 1.00 s |
| to-primary | otros | 1.00 s |

## Verificación del libro mayor

| Ejecución | Movimientos iniciales | Esperados | Almacenados | Banda de incertidumbre | Saldo dentro del rango | Consistente | Claves duplicadas |
|---|---|---|---|---|---|---|---|
| 1 | 10 | 69 | 69 | 0 | sí | sí | 0 |

## Hallazgos

Los hallazgos los produce un conjunto fijo de reglas a partir de los datos medidos de cada ejecución.

### Ejecución 1: 2026-11-11T12-00-00Z

- Veredicto del runner: PASS.
- `to-secondary`: las escrituras no estuvieron disponibles durante 58.05 s y el RTO de escritura fue 60.00 s.
- `to-secondary`: las lecturas se recuperaron en 70.00 s (RTO de lectura).
- `to-secondary`: la mayor parte del tiempo del workflow se fue en switchover, 12 s de 45 s gastados en las categorías medidas.
- `to-secondary`: la primera respuesta DNS distinta se vio 1.00 s después de que terminó el paso de cambio de tráfico.
- `to-secondary`: el inicio de sesión en la región secundaria tuvo éxito en 66.7 % de los intentos durante la ventana de la caída (2 de 6 fallaron); la racha más larga de fallos consecutivos fue de 2 inicios de sesión y duró 20.04 s.
- En este sentido el workflow ejecuta el paso Group 2 - activate the Cognito replica in the secondary region (5 s), que cambia el estado de la réplica de Cognito en la región secundaria, así que los inicios de sesión fallidos allí son en parte intencionales y esta cifra no es la disponibilidad del servicio para un usuario.
- `to-primary`: las escrituras no estuvieron disponibles durante 8.05 s y el RTO de escritura fue 10.00 s.
- `to-primary`: el RTO de lectura no se midió (no se vio ninguna solicitud fallida o reintentada en la ventana (no se observó caída)).
- `to-primary`: la mayor parte del tiempo del workflow se fue en switchover, 7 s de 19 s gastados en las categorías medidas.
- `to-primary`: la primera respuesta DNS distinta se vio 1.00 s después de que terminó el paso de cambio de tráfico.
- `to-primary`: el inicio de sesión en la región secundaria tuvo éxito en 100.0 % de los intentos durante la ventana de la caída (0 de 3 fallaron); la racha más larga de fallos consecutivos fue de 0 inicios de sesión y duró 0.00 s.
- En este sentido el workflow ejecuta el paso Group 2 - deactivate the Cognito replica in the secondary region (2 s), que cambia el estado de la réplica de Cognito en la región secundaria, así que los inicios de sesión fallidos allí son en parte intencionales y esta cifra no es la disponibilidad del servicio para un usuario.
- Depósitos de prueba de los failovers: to-secondary: registrado (+1 seguro); to-primary: OMITIDO (SKIPPED, no registrado).
- Los depósitos de prueba OMITIDOS (SKIPPED) no se registran y no suman nada al libro mayor.
- RPO medido: 0 escrituras confirmadas perdidas (69 movimientos almacenados, 69 esperados, banda de incertidumbre 0). Es la ruta del switchover planificado; no dice nada sobre un failover no planificado.
- Retraso de replicación (AuroraGlobalDBReplicationLag) durante la ejecución: máximo 1300.0 ms, percentil 95 1300.0 ms, promedio 1042.5 ms sobre 4 muestras de un período de 60 s. Es una cota de lo que un failover no planificado podría perder en ese momento, no una medición del RPO de un failover no planificado, que este simulacro no ejercita.

## Limitaciones

- Una sola ejecución desde una estación de trabajo y un camino de red: los números son una muestra, no una distribución.
- El DNS se observa desde la estación de trabajo con un solo resolvedor, cada 2 s. No se miden otros resolvedores, navegadores ni cachés.
- Solo se ejerce la ruta planificada (switchover con la región primaria simulada como no disponible al limitar sus funciones). Un failover no planificado, con su propio RPO, no se mide; el retraso de replicación es solo una cota.
- El cambio es un workflow manual que lanza el runner. No se miden el tiempo de detección ni el que una persona tarda en decidir; el tiempo de decisión de las tablas es el del runner.
- La carga es sintética y neta en cero: cerca de una escritura cada 1 s, muy por debajo del tráfico de producción.
- La resolución la limitan las sondas: escrituras cerca de 1 s, lecturas y salud 5 s, inicio de sesión 10 s, DNS 2 s, marcas de tiempo de GitHub 1 s, retraso de replicación un dato cada 60 s.
- El retraso de replicación se lee tras esperar 120 s a CloudWatch; los últimos 1 a 3 minutos de retraso aún pueden faltar.
- Los tiempos de los workflows vienen de GitHub y los de las sondas del reloj de la estación de trabajo; el desfase entre ambos no se corrige.
- El RPO se comprueba contra el libro mayor de los movimientos de la sonda; los movimientos sin confirmar pueden haberse registrado o no (banda de incertidumbre).

## Fuentes

Carpetas de ejecución (locales, sin versionar): `2026-11-11T12-00-00Z`. Fecha de la prueba (UTC): 2026-11-11.
