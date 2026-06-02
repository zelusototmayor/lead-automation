# Logistics ES — Quote Agent: 4-email sequence

Approved by Zelu on 2026-04-26. Verbatim. Do NOT edit copy without re-approval.

- Sender inbox: `jose@zelusotto.com`
- Cadence: Day 0 / Day 4 / Day 9 / Day 14 (Instantly delays: 0/4/5/5)
- Send window: Tue/Wed/Thu, 09:00–10:00 Europe/Madrid (skip weekends)
- Daily limit: 10
- Open tracking OFF, link tracking OFF
- Merge fields: `{{first_name}}`, `{{company}}`, `{{city}}`
- Hard rule: no URLs in body; only `max@zelusottomayor.com` as the try-it address

---

## Email 1 — Day 0

**Subject:** `cotizaciones en {{company}}`

```
Hola {{first_name}},

Estuve investigando empresas de transporte por carretera en {{city}} y {{company}} apareció varias veces. Le escribo por algo concreto.

En empresas como la suya, las cotizaciones que no salen el mismo día se pierden — el cliente llama al siguiente transportista. Lo que normalmente significa que usted, o quien esté cotizando, acaba respondiendo a horas en las que debería estar haciendo otra cosa.

He construido un agente de IA que lee esas solicitudes y prepara la cotización con sus tarifas y rutas. La revisan antes de enviar — el criterio sigue siendo suyo. Lo que desaparece son los minutos repetitivos.

Reenvíe una solicitud real a max@zelusottomayor.com y le devuelvo lo que prepararía el agente. Para que la compare con la suya antes de cualquier conversación.

Un saludo,
Zelu
```

## Email 2 — Day 4 (reply to thread)

**Subject:** `re: cotizaciones en {{company}}`

```
{{first_name}},

Imagino que la semana ha sido intensa. Le dejo el detalle que suele importar más a directores de operaciones:

Nada se envía automáticamente. El agente prepara el borrador con sus tarifas y su forma de redactar — pero la decisión final siempre la toma una persona del equipo. Lo que cambia es el tiempo: en vez de empezar cada cotización desde cero, su equipo revisa una propuesta que ya está casi lista.

Si quiere probarlo, reenvíe una solicitud real a max@zelusottomayor.com.

Un saludo,
Zelu
```

## Email 3 — Day 9 (different angle)

**Subject:** `una idea distinta`

```
{{first_name}},

Cambio el enfoque, porque el anterior puede no ser el adecuado.

Si el volumen de cotizaciones en {{company}} no es alto, esto probablemente no le aporta nada. Pero si su equipo está respondiendo a más de 30 o 40 solicitudes a la semana — y especialmente si algunas se quedan sin respuesta porque no hay tiempo material — el agente cambia esa dinámica. No por sustituir al equipo, sino por dejarles atender lo que de verdad necesita criterio humano.

No le pido una llamada. Si tiene curiosidad, reenvíe una solicitud a max@zelusottomayor.com y vea lo que devuelve. Si no encaja, lo entiendo perfectamente.

Un saludo,
Zelu
```

## Email 4 — Day 14 (breakup)

**Subject:** `cierro este hilo`

```
{{first_name}},

No quiero llenarle el buzón. Cierro aquí.

Si en algún momento quiere ver cómo respondería el agente a una cotización real de {{company}}, max@zelusottomayor.com está abierto. Sin agenda comercial, sólo el ejercicio.

Buena ruta,
Zelu
```
