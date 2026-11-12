# Informe de la prueba de DR

Versión en inglés (fuente): [docs/dr-test-report.md](../docs/dr-test-report.md). Esta es una copia en español; si difieren, manda el inglés.

Fecha de la prueba (UTC): 2026-11-11.

Generado con `python -m dr_drill formal-report` a partir del `timeline.json` de 1 carpeta(s) de ejecución: `2026-11-11T19-49-33Z`. Todos los números salen de esas líneas de tiempo; ninguno se escribe a mano.

## Propósito

Medir, con un script repetible, cuánto tarda la demo en volver a atender desde la segunda región en cada sentido (RTO), si se pierde alguna escritura confirmada (RPO del switchover planificado) y en qué se va el tiempo. La meta de RTO de 15 minutos del proyecto es una meta, no un resultado de este informe.

## Método

Un runner en una estación de trabajo maneja el entorno prod real mediante la API pública y los workflows de GitHub. Fases, en orden: `baseline` (carga estable sobre la región primaria), `fence` (el workflow `simulate-outage` limita a cero las funciones de la región primaria), `to-secondary` (el workflow `failover` promueve la región secundaria), `steady-secondary`, `to-primary` (el workflow `failover` vuelve a la primaria), `steady-primary` y `consistency` (verificación final del libro mayor). Cuatro sondas corren durante todo el simulacro: una de escritura (un intento cada 1 s, depósitos y retiros de 0.01 alternados, de modo que el libro queda neto en cero; un movimiento sin confirmar se reintenta con la misma clave de idempotencia), una de lectura (`GET /account` cada 5 s), una de salud (`GET /health?deep=true` cada 5 s) y una de inicio de sesión contra la región secundaria (cada 10 s). Un observador de DNS consulta el nombre de la API cada 2 s desde la estación de trabajo. El retraso de replicación se lee de CloudWatch al terminar, después de que el runner espera 120 s por los últimos datos. Las horas son UTC. Los percentiles son de rango más cercano. El simulacro lanza los workflows con un script; el cambio en sí es el cambio manual de enrutamiento ponderado del proyecto, ejecutado por el workflow.

## Entorno

- Regiones: primaria `us-east-2` (escritora al inicio), secundaria `us-west-2`.
- Base de datos: Aurora PostgreSQL Global Database, clase de instancia `db.r6g.large`, versión del motor `16.15` (leídas de `docs/technical.md`).
- Tráfico: registros ponderados de Route 53 que cambia el workflow `failover`; las sondas llaman al nombre público de la API, así que recorren el mismo camino que un usuario.
- Este informe no incluye datos de cuenta.

## Resultados principales

| Ejecución | Fecha (UTC) | Veredicto | RTO de escritura to-secondary | RTO de escritura to-primary | RPO medido |
|---|---|---|---|---|---|
| 1 | 2026-11-11 | PASS | 196.82 s | 82.68 s | 0 escrituras confirmadas perdidas |

## Métricas

Una métrica sin datos se informa como no medida, nunca como cero.

### Ejecución 1: 2026-11-11T19-49-33Z

| Métrica | Valor | Definición | Fuente | Resolución |
|---|---|---|---|---|
| to-secondary: RTO de escritura | 196.82 s | Última escritura 2xx respondida por us-east-2 antes de la primera escritura fallida o reintentada, hasta la primera escritura 2xx respondida por us-west-2. | sonda de escritura, un intento cada 1 s | cerca de 1 s |
| to-secondary: RTO de lectura | 202.43 s | Lo mismo con `GET /account`: última respuesta 2xx de us-east-2 antes de la primera lectura fallida, hasta la primera 2xx de us-west-2. | sonda de lectura, `GET /account` cada 5 s | cerca de 5 s |
| to-secondary: indisponibilidad de escritura | 196.18 s | Primera escritura fallida hasta la primera escritura 2xx respondida por us-west-2. | sonda de escritura, un intento cada 1 s | cerca de 1 s |
| to-secondary: disponibilidad del inicio de sesión en la secundaria | 70.0 % | Proporción de inicios de sesión exitosos en la región secundaria desde el inicio de la caída hasta el fin de la fase estable (30 intentos, 9 fallidos). | sonda de inicio de sesión, uno cada 10 s | 10 s |
| to-secondary: racha más larga de inicios de sesión fallidos | 9 inicios de sesión, 90.58 s | Racha más larga de inicios de sesión fallidos consecutivos y su duración. | sonda de inicio de sesión, uno cada 10 s | 10 s |
| to-secondary: convergencia de DNS | -43.69 s | Fin del paso de cambio de tráfico hasta la primera respuesta DNS distinta (negativa si la respuesta cambió antes de que terminara el paso). | observador de DNS y los tiempos de los pasos de GitHub | 2 s, un resolvedor |
| to-secondary: tiempo de decisión | 10.87 s | Fin de la ejecución de fence hasta el lanzamiento del failover. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| to-primary: RTO de escritura | 82.68 s | Última escritura 2xx respondida por us-west-2 antes de la primera escritura fallida o reintentada, hasta la primera escritura 2xx respondida por us-east-2. | sonda de escritura, un intento cada 1 s | cerca de 1 s |
| to-primary: RTO de lectura | 85.93 s | Lo mismo con `GET /account`: última respuesta 2xx de us-west-2 antes de la primera lectura fallida, hasta la primera 2xx de us-east-2. | sonda de lectura, `GET /account` cada 5 s | cerca de 5 s |
| to-primary: indisponibilidad de escritura | 82.45 s | Primera escritura fallida hasta la primera escritura 2xx respondida por us-east-2. | sonda de escritura, un intento cada 1 s | cerca de 1 s |
| to-primary: disponibilidad del inicio de sesión en la secundaria | 43.3 % | Proporción de inicios de sesión exitosos en la región secundaria desde el inicio de la caída hasta el fin de la fase estable (30 intentos, 17 fallidos). | sonda de inicio de sesión, uno cada 10 s | 10 s |
| to-primary: racha más larga de inicios de sesión fallidos | 17 inicios de sesión, 190.51 s | Racha más larga de inicios de sesión fallidos consecutivos y su duración. | sonda de inicio de sesión, uno cada 10 s | 10 s |
| to-primary: convergencia de DNS | -49.70 s | Fin del paso de cambio de tráfico hasta la primera respuesta DNS distinta (negativa si la respuesta cambió antes de que terminara el paso). | observador de DNS y los tiempos de los pasos de GitHub | 2 s, un resolvedor |
| to-primary: tiempo de decisión | no medido: no aplica: el regreso se lanza tras una fase estable de duración fija | Fin de la ejecución de fence hasta el lanzamiento del failover. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| simulate-outage (fence): tiempo en cola | 0.78 s | Lanzamiento hasta el inicio de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| simulate-outage (fence): tiempo de ejecución | 32.00 s | Inicio de la ejecución hasta su última actualización. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| simulate-outage (fence): total | 32.78 s | Lanzamiento hasta el fin de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-secondary): tiempo en cola | 1.13 s | Lanzamiento hasta el inicio de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-secondary): tiempo de ejecución | 222.00 s | Inicio de la ejecución hasta su última actualización. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-secondary): total | 223.13 s | Lanzamiento hasta el fin de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-primary): tiempo en cola | 0.60 s | Lanzamiento hasta el inicio de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-primary): tiempo de ejecución | 235.00 s | Inicio de la ejecución hasta su última actualización. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| failover (to-primary): total | 235.60 s | Lanzamiento hasta el fin de la ejecución. | tiempos de ejecución de GitHub y hora del lanzamiento | 1 s (marcas de tiempo de GitHub) |
| AuroraGlobalDBReplicationLag: máximo | 933.0 ms | Máximo de las 12 muestras tomadas durante la ejecución (milisegundos). | CloudWatch AuroraGlobalDBReplicationLag, máximo por período | período de 60 s |
| AuroraGlobalDBReplicationLag: percentil 95 | 933.0 ms | Percentil 95 de las 12 muestras tomadas durante la ejecución (milisegundos). | CloudWatch AuroraGlobalDBReplicationLag, máximo por período | período de 60 s |
| AuroraGlobalDBReplicationLag: promedio | 686.5 ms | Promedio de las 12 muestras tomadas durante la ejecución (milisegundos). | CloudWatch AuroraGlobalDBReplicationLag, máximo por período | período de 60 s |
| AuroraGlobalDBRPOLag: máximo | 1531.0 ms | Máximo de las 14 muestras tomadas durante la ejecución (milisegundos). | CloudWatch AuroraGlobalDBRPOLag, máximo por período | período de 60 s |
| AuroraGlobalDBRPOLag: percentil 95 | 1531.0 ms | Percentil 95 de las 14 muestras tomadas durante la ejecución (milisegundos). | CloudWatch AuroraGlobalDBRPOLag, máximo por período | período de 60 s |
| AuroraGlobalDBRPOLag: promedio | 854.4 ms | Promedio de las 14 muestras tomadas durante la ejecución (milisegundos). | CloudWatch AuroraGlobalDBRPOLag, máximo por período | período de 60 s |

## Línea de tiempo

Los desfases son segundos desde el inicio de cada sentido (el lanzamiento de la ejecución `fence` para `to-secondary`; el inicio de la fase `to-primary` para `to-primary`). Se omiten los pasos de workflow más cortos que la resolución de las marcas de tiempo de GitHub.

### Fases de la ejecución 1

| Fase | Inicio (UTC) | Fin (UTC) | Duración |
|---|---|---|---|
| baseline | 2026-11-11T19:49:44.204Z | 2026-11-11T19:50:44.218Z | 60.0 s |
| fence | 2026-11-11T19:50:44.221Z | 2026-11-11T19:51:27.871Z | 43.6 s |
| to-secondary | 2026-11-11T19:51:27.872Z | 2026-11-11T19:55:25.384Z | 237.5 s |
| steady-secondary | 2026-11-11T19:55:25.385Z | 2026-11-11T19:56:25.395Z | 60.0 s |
| to-primary | 2026-11-11T19:56:25.397Z | 2026-11-11T20:00:56.109Z | 270.7 s |
| steady-primary | 2026-11-11T20:00:56.111Z | 2026-11-11T20:01:56.117Z | 60.0 s |
| consistency | 2026-11-11T20:01:56.118Z | 2026-11-11T20:02:02.067Z | 5.9 s |

### Ejecución 1: to-secondary (fence y luego failover a la región secundaria)

| Desfase | Hora (UTC) | Evento |
|---|---|---|
| +0.0 s | 2026-11-11T19:50:44.221Z | Empieza la fase `fence` |
| +0.0 s | 2026-11-11T19:50:44.223Z | Lanzamiento de `simulate-outage` (fence) |
| +0.8 s | 2026-11-11T19:50:45.000Z | `simulate-outage` (fence) empieza en el runner |
| +14.8 s | 2026-11-11T19:50:59.000Z | Paso Run 10-simulate-outage.sh (bloqueo (fence), 13 s) |
| +23.1 s | 2026-11-11T19:51:07.362Z | Primer intento de escritura fallido |
| +32.8 s | 2026-11-11T19:51:17.000Z | Termina `simulate-outage` (fence) |
| +43.7 s | 2026-11-11T19:51:27.872Z | Lanzamiento de `failover` (to-secondary) |
| +44.8 s | 2026-11-11T19:51:29.000Z | `failover` (to-secondary) empieza en el runner |
| +55.8 s | 2026-11-11T19:51:40.000Z | Paso Group 1 - pre-checks (read only) (verificaciones previas, 20 s) |
| +75.8 s | 2026-11-11T19:52:00.000Z | Paso Group 1 - confirm that the primary region is fenced (verificaciones previas, 6 s) |
| +81.8 s | 2026-11-11T19:52:06.000Z | Paso Group 1 - confirm that the secondary region is NOT fenced (verificaciones previas, 5 s) |
| +88.8 s | 2026-11-11T19:52:13.000Z | Paso Group 2 - activate the Cognito replica in the secondary region (réplica de Cognito, 40 s) |
| +128.8 s | 2026-11-11T19:52:53.000Z | Paso Group 2 - planned switchover to the secondary region (switchover, 54 s) |
| +183.8 s | 2026-11-11T19:53:48.000Z | Paso Group 3 - switch the Route 53 weights (cambio de tráfico, 76 s) |
| +216.1 s | 2026-11-11T19:54:20.313Z | Primera respuesta DNS distinta tras empezar el cambio de tráfico (un resolvedor) |
| +219.3 s | 2026-11-11T19:54:23.540Z | Primera escritura respondida por us-west-2 |
| +259.8 s | 2026-11-11T19:55:04.000Z | Paso Group 3 - validate the service on the custom domain (validación, 1 s) |
| +266.8 s | 2026-11-11T19:55:11.000Z | Termina `failover` (to-secondary) |
| +281.2 s | 2026-11-11T19:55:25.385Z | Empieza la fase estable `steady-secondary` |

### Ejecución 1: to-primary (regreso a la región primaria)

| Desfase | Hora (UTC) | Evento |
|---|---|---|
| +0.0 s | 2026-11-11T19:56:25.397Z | Empieza la fase `to-primary` |
| +0.0 s | 2026-11-11T19:56:25.398Z | Lanzamiento de `failover` (to-primary) |
| +0.6 s | 2026-11-11T19:56:26.000Z | `failover` (to-primary) empieza en el runner |
| +12.6 s | 2026-11-11T19:56:38.000Z | Paso Group 1 - unfence the primary region (bloqueo (fence), 17 s) |
| +30.6 s | 2026-11-11T19:56:56.000Z | Paso Group 2 - planned switchover back to the primary region (switchover, 85 s) |
| +100.2 s | 2026-11-11T19:58:05.633Z | Primer intento de escritura fallido |
| +115.6 s | 2026-11-11T19:58:21.000Z | Paso Group 2 - deactivate the Cognito replica in the secondary region (réplica de Cognito, 40 s) |
| +156.6 s | 2026-11-11T19:59:02.000Z | Paso Group 3 - switch the Route 53 weights (cambio de tráfico, 74 s) |
| +180.9 s | 2026-11-11T19:59:26.303Z | Primera respuesta DNS distinta tras empezar el cambio de tráfico (un resolvedor) |
| +182.7 s | 2026-11-11T19:59:28.087Z | Primera escritura respondida por us-east-2 |
| +230.6 s | 2026-11-11T20:00:16.000Z | Paso Group 3 - validate the service on the custom domain (validación, 1 s) |
| +235.6 s | 2026-11-11T20:00:21.000Z | Termina `failover` (to-primary) |
| +270.7 s | 2026-11-11T20:00:56.111Z | Empieza la fase estable `steady-primary` |

### Ejecución 1: tiempo por categoría de paso del workflow

| Sentido | Categoría | Duración |
|---|---|---|
| to-secondary | cola | 1.90 s |
| to-secondary | preparación | 20.00 s |
| to-secondary | verificaciones previas | 31.00 s |
| to-secondary | bloqueo (fence) | 13.00 s |
| to-secondary | réplica de Cognito | 40.00 s |
| to-secondary | switchover | 54.00 s |
| to-secondary | cambio de tráfico | 76.00 s |
| to-secondary | validación | 1.00 s |
| to-secondary | verificación de consistencia | 0.00 s |
| to-primary | cola | 0.60 s |
| to-primary | preparación | 9.00 s |
| to-primary | verificaciones previas | 0.00 s |
| to-primary | bloqueo (fence) | 17.00 s |
| to-primary | réplica de Cognito | 40.00 s |
| to-primary | switchover | 85.00 s |
| to-primary | cambio de tráfico | 74.00 s |
| to-primary | validación | 1.00 s |
| to-primary | verificación de consistencia | 0.00 s |

## Verificación del libro mayor

| Ejecución | Movimientos iniciales | Esperados | Almacenados | Banda de incertidumbre | Saldo dentro del rango | Consistente | Claves duplicadas |
|---|---|---|---|---|---|---|---|
| 1 | 67 | 504 | 504 | 0 | sí | sí | 0 |

## Hallazgos

Los hallazgos los produce un conjunto fijo de reglas a partir de los datos medidos de cada ejecución.

### Ejecución 1: 2026-11-11T19-49-33Z

- Veredicto del runner: PASS.
- `to-secondary`: las escrituras no estuvieron disponibles durante 196.18 s y el RTO de escritura fue 196.82 s.
- `to-secondary`: las lecturas se recuperaron en 202.43 s (RTO de lectura).
- `to-secondary`: la mayor parte del tiempo del workflow se fue en cambio de tráfico, 76 s de 215 s gastados en las categorías medidas.
- `to-secondary`: la convergencia de DNS es negativa (-43.69 s): la respuesta de DNS cambió antes de que terminara el paso de cambio de tráfico. Se informa tal como se midió, sin corregirla.
- `to-secondary`: el inicio de sesión en la región secundaria tuvo éxito en 70.0 % de los intentos durante la ventana de la caída (9 de 30 fallaron); la racha más larga de fallos consecutivos fue de 9 inicios de sesión y duró 90.58 s.
- En este sentido el workflow ejecuta el paso Group 2 - activate the Cognito replica in the secondary region (40 s), que cambia el estado de la réplica de Cognito en la región secundaria, así que los inicios de sesión fallidos allí son en parte intencionales y esta cifra no es la disponibilidad del servicio para un usuario.
- `to-primary`: las escrituras no estuvieron disponibles durante 82.45 s y el RTO de escritura fue 82.68 s.
- `to-primary`: las lecturas se recuperaron en 85.93 s (RTO de lectura).
- `to-primary`: la mayor parte del tiempo del workflow se fue en switchover, 85 s de 217 s gastados en las categorías medidas.
- `to-primary`: la convergencia de DNS es negativa (-49.70 s): la respuesta de DNS cambió antes de que terminara el paso de cambio de tráfico. Se informa tal como se midió, sin corregirla.
- `to-primary`: el inicio de sesión en la región secundaria tuvo éxito en 43.3 % de los intentos durante la ventana de la caída (17 de 30 fallaron); la racha más larga de fallos consecutivos fue de 17 inicios de sesión y duró 190.51 s.
- En este sentido el workflow ejecuta el paso Group 2 - deactivate the Cognito replica in the secondary region (40 s), que cambia el estado de la réplica de Cognito en la región secundaria, así que los inicios de sesión fallidos allí son en parte intencionales y esta cifra no es la disponibilidad del servicio para un usuario.
- La respuesta del resolvedor cambió 9 veces durante toda la ejecución para 2 cambios de tráfico, de modo que un cambio de respuesta no se puede atribuir solo al cambio de tráfico y la convergencia de DNS debe leerse como un indicio, no como una prueba.
- Depósitos de prueba de los failovers: to-secondary: OMITIDO (SKIPPED, no registrado); to-primary: OMITIDO (SKIPPED, no registrado).
- Los dos depósitos de prueba de los failovers quedaron OMITIDOS (SKIPPED), según la clasificación del log del paso de validación. Un depósito omitido no se registra y no suma nada al libro mayor, y la validación del failover no ejercitó una escritura autenticada.
- Causa esperada: `failover.yml` no da al paso de validación un token de Cognito.
- RPO medido: 0 escrituras confirmadas perdidas (504 movimientos almacenados, 504 esperados, banda de incertidumbre 0). Es la ruta del switchover planificado; no dice nada sobre un failover no planificado.
- Retraso de replicación (AuroraGlobalDBReplicationLag) durante la ejecución: máximo 933.0 ms, percentil 95 933.0 ms, promedio 686.5 ms sobre 12 muestras de un período de 60 s. Es una cota de lo que un failover no planificado podría perder en ese momento, no una medición del RPO de un failover no planificado, que este simulacro no ejercita.
- Retraso de replicación (AuroraGlobalDBRPOLag) durante la ejecución: máximo 1531.0 ms, percentil 95 1531.0 ms, promedio 854.4 ms sobre 14 muestras de un período de 60 s. Es una cota de lo que un failover no planificado podría perder en ese momento, no una medición del RPO de un failover no planificado, que este simulacro no ejercita.
- El filtro de salida del runner rechazó 12 registros de las sondas y no están en la línea de tiempo, así que los conteos de intentos y las estadísticas que salen de ellos cubren algo menos de intentos que los que hicieron las sondas.

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

Carpetas de ejecución (locales, sin versionar): `2026-11-11T19-49-33Z`. Fecha de la prueba (UTC): 2026-11-11.
