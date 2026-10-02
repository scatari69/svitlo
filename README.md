# Svitlo

Python 3.13+ service for a Ukrainian Telegram electricity bot and HTTP API.

See [the architecture review](ARCHITECTURE.md) for component boundaries, data flows,
refactoring results and remaining operational limits.

## Start with Docker

```bash
cp .env.example .env
# Edit .env: replace both password placeholders; use URL-safe passwords.
# To enable Telegram, set TELEGRAM_BOT_TOKEN and BOT_ENABLED=true.
docker compose build
docker compose run --rm app alembic upgrade head
docker compose up -d
curl http://localhost:8000/health
curl http://localhost:8000/readiness
```

The example starts in API-only mode. PostgreSQL and Redis have persistent volumes,
authentication, and health checks. Published ports bind to localhost. The app runs
as a non-root user; SIGTERM stops polling and closes all clients.

```bash
docker compose logs -f app
docker compose down
```

`down` keeps stored data. Run one application instance/worker per Telegram token:
Telegram long polling does not support multiple polling processes for one bot.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
# Edit .env as above. Local URLs use localhost; URL-encode passwords if needed.
docker compose up -d postgres redis
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Avoid auto-reload with polling enabled, since overlapping workers can conflict.
Dependency ranges and development extras are maintained in `pyproject.toml`.

```bash
pytest
ruff check .
mypy .
```

Tests mock external systems; they need no PostgreSQL, Redis, or Telegram connection.

## Layout and boundaries

- `app/bot/`: aiogram polling, thin handlers, and Ukrainian inline keyboards.
- `app/api/`: thin FastAPI routes.
- `app/services/`: owner-scoped device setup, encryption, webhooks, and health probes.
- `app/db/`: async SQLAlchemy engine, session factory, metadata, and UTC timestamp type.
- `app/models/`: monitoring, notification, schedule, and report tables.
- `app/repositories/`: owner-scoped async queries and idempotent interval writes.
- `app/events/`: immutable domain events, async dispatch, and safe event logging.
- `app/monitoring/`: actual electricity state, independent of planned schedules.
- `app/schedules/`: planned outage data, independent of actual monitoring.
- `app/analytics/`: period statistics and Ukrainian presentation from actual power history.
- `app/notifications/`: power/schedule event subscribers, Ukrainian messages, and delivery.
- `migrations/`: Alembic async migration environment and revision template.
- `tests/`: schema, events, backend probes, device services, FSM, API, and lifecycle checks.

The device menu is implemented with guided configuration and connection probes.
Schedule fetching and subscription management are implemented; channel registration remains a future feature.
Power notifications consume domain events independently of monitoring backends. The production dispatcher uses Redis FSM storage
with 30-minute expiry and event isolation; standalone tests use memory storage.

`/health` returns HTTP 200 while the process is serving, reporting `process`,
`database`, and `redis` statuses. Failed dependencies produce `status: degraded`.
`/readiness` returns the same body and HTTP 503 when either dependency is unavailable.
Probes run concurrently with a configurable timeout (`HEALTH_TIMEOUT`, seconds).
Responses contain no connection strings, credentials, or exception details.
Dependency outages do not prevent the API from starting.

Settings load from environment variables and `.env`. JSON logs use UTC timestamps,
redact configured connection URLs/passwords/token, and record exception types
without exception messages or tracebacks that could contain secrets. Never log
full settings or backend configuration objects.

## Migrations

Revision `0001` creates the initial 12-table schema. Revision `0002` adds the power
notification delivery ledger. Models use `app.db.base.Base`;
`app.models` registers all tables in the Alembic environment.
Create a new migration for every schema change and review it before applying:

```bash
alembic revision --autogenerate -m "Describe schema change"
alembic upgrade head
# Or against the Compose database:
docker compose run --rm app alembic upgrade head
```

Migrations are explicit, never run automatically by every application worker.

## Database contracts

Telegram IDs use signed 64-bit columns. Timestamp columns compile to PostgreSQL
`TIMESTAMP WITH TIME ZONE`; the ORM rejects naive inputs and normalizes values to UTC.
Enums use named CHECK constraints so their values can evolve through normal migrations.

Devices keep actual ON/OFF/UNKNOWN state and monitor health separately. Power intervals
include UNKNOWN to preserve gaps in actual data. Their duration is calculated from
aware timestamps, with an explicit end time required for open intervals. A partial
unique index enforces one open interval per device. Interval writes lock the device,
ignore repeated states, reject backwards timestamps, and flush interval closure before
inserting the next interval. Repositories never commit: use a caller-owned transaction,
for example `async with session_factory.begin() as session:`.

Device configurations are one-to-one and constrained to the matching monitoring type.
Notification links use composite foreign keys to enforce a shared owner. Report settings
belong to an existing device/channel link, with daily, weekly, and monthly flags.
Schedule versions are shared source snapshots without device/subscription dependencies;
repeated content after an intervening change can have a new version.

Users with owned records and devices with power history cannot be hard-deleted.
Devices support soft deletion through `deleted_at`. Backend configurations and
notification links cascade on deliberate hard deletion of their parent. Deleting a
channel/link removes report settings, but preserves device history. Subscription deletion
removes notification links while retaining shared schedule versions. Downgrading `0001`
is destructive: it drops all initial tables and their data.

SNMP communities and Home Assistant access tokens have ciphertext-only binary columns;
webhook credentials store unique SHA-256 token hashes. Fernet encrypts secrets
before they enter FSM storage or the database; keep `ENCRYPTION_KEY` stable and
back it up separately. Changing it makes existing ciphertext unreadable.
Model representations contain only the class name, and
SQLAlchemy engines hide bound parameters in diagnostics.

Database unit tests use SQLite constraints and mocked async transport backed by real
SQL execution. PostgreSQL DDL compilation and Alembic upgrade/downgrade SQL are also
checked. Live PostgreSQL locking/concurrency still requires integration validation.

## Domain events

The application lifespan exposes `app.state.event_bus`, with the actual-history
subscriber and safe logging subscriber registered. Monitoring producers publish
`PowerStateChanged`; they do not import Telegram or know which consumers are attached.
`MonitorHealthChanged` and `ScheduleChanged` also support typed subscriptions; schedule
notification delivery is registered when Telegram is enabled. All event timestamps
must be aware and are normalized to UTC.

```python
from datetime import UTC, datetime

from app.events.models import PowerStateChanged
from app.models.enums import PowerState

await app.state.event_bus.publish(
    PowerStateChanged(
        user_id=user_id,
        device_id=device_id,
        previous_state=PowerState.ON,
        new_state=PowerState.OFF,
        detected_at=datetime.now(UTC),
    )
)
```

Publication awaits matching handlers in registration order. Subscribing the same
handler twice has no effect. Ordinary handler failures do not suppress other
subscribers; publication raises an `ExceptionGroup` afterwards. Cancellation
propagates immediately. There are no background dispatch tasks to drain at shutdown.

The history subscriber owns a transaction and reuses `PowerIntervalRepository`.
It locks and refreshes the device, validates the event's predecessor, closes the
previous interval, and opens the new ON/OFF/UNKNOWN interval. Repeated states leave
history and the last-change timestamp untouched. Exact persisted state/timestamp
replays are harmless even after subsequent transitions or application restart.
Unrecorded backwards transitions and ambiguous conflicting states at the same
timestamp are rejected. Event UUIDs identify logs; database history provides
idempotency. UNKNOWN intervals remain separate from confirmed ON/OFF durations.

When Telegram is enabled, `PowerNotificationHandler` is registered after the history
subscriber and sends power notifications to each enabled channel assigned to the device.
Actual analytics is exposed through `app.state.analytics`; the report worker delivers
scheduled reports when Telegram is enabled.

The bus is in-process and has no durable queue, retries, or outbox. Successfully
persisted interval history survives restarts; dispatch itself is not guaranteed
across process failure. Power notifications use durable per-transition delivery
reservations. A transactional outbox is still needed if delivery must survive failure
between history commit and dispatch.

## Device management

Open **⚙️ Пристрої** in a private chat. The wizard supports:

- Ping: host, interval, timeout, failure threshold (minimum two), recovery threshold.
- SNMP v2c: host, port, community, interval, interface index or numeric custom OID
  with distinct ON/OFF values.
- Home Assistant WebSocket (advanced API mode): URL, long-lived token, entity ID,
  and ON/OFF state mapping. Other states remain explicitly UNKNOWN.
- Home Assistant webhook: a random URL displayed once inside a copyable Ukrainian
  Home Assistant YAML example. Replace `binary_sensor.power`, merge the REST command
  and automation into `configuration.yaml`, restart HA, then test and save. Setup expires
  after 30 minutes. Only the URL hash is persisted, and access logs redact the token.

Configure `PUBLIC_BASE_URL` to an HTTPS address reachable from Home Assistant.
Forward `/api/v1/homeassistant/webhook/` through your HTTPS reverse proxy; configure
proxy access logs to redact webhook paths too. The example's localhost address is
for development. Webhooks are limited to 30 requests per token per minute with Redis;
dependency failure does not bypass the limiter.

Before SNMP or Home Assistant API setup, generate a key and put it in `.env`:

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

The wizard requires a successful probe and rechecks on save. Ping confirms recovery
across consecutive successful packets. Unknown SNMP/HA values fail verification;
a transport failure remains UNKNOWN/UNAVAILABLE. Credentials are removed from
incoming Telegram messages when Telegram permissions allow, never echoed in menus,
and encrypted before FSM persistence. Hostnames, URLs, numeric ranges, OIDs, and
entity IDs are validated. LAN targets are intentionally allowed for home devices.
Ping uses a fixed subprocess argument list; install `iputils-ping` for local use
(the Docker image includes it). HTTP probes use timeouts and reject redirects.

Device details show Kyiv times, current state, backend, last state change and last
successful check. Configuration can rename or replace connection settings within
the same backend. The channels button lists existing assignments; assigning channels
belongs to the channel-management feature. Delete requires confirmation, closes the
open interval, stops the device and retains history. All service operations are scoped
to the Telegram user; duplicate save/delete callbacks cannot repeat completed actions.

Diagnostic/setup probes do not change actual power history. Received webhooks do:
they update health/check timestamps and use locked idempotent interval writes before
publishing domain events. The webhook test itself does not create a device or interval.
Ping polling and Home Assistant WebSocket subscriptions run automatically when
monitoring is enabled. SNMP background polling remains a future runtime worker.
No schema migration is needed: existing backend configuration tables are reused.

## Ping monitoring

`MONITORING_ENABLED=true` (default) starts the asynchronous Ping scheduler independently
of Telegram polling. Every five seconds it discovers enabled, configured Ping devices
across all users. Disabled/deleted devices stop; connection-setting changes restart
only the affected task. Results for changed or retired configurations are discarded
under the device row lock, including probes already in flight when settings changed.

Each device has its own non-overlapping timer using the configured interval (default
10 seconds). At most 32 checks run concurrently. PostgreSQL selects one scheduler
leader with a session advisory lock on a dedicated connection; followers wait and retry.
The lock is explicitly released before returning the connection to the pool. Database
failure cancels active tasks and retries discovery; API startup does not depend on
successful worker startup. Shutdown cancels tasks and reaps active Ping subprocesses.

The Ping adapter uses `asyncio.create_subprocess_exec`, a fixed argument list, validated
hostnames/IPs, a response timeout (default 2 seconds), and a bounded process deadline.
No shell is involved. Ordinary no-reply results must occur consecutively three times
before confirming OFF; two consecutive replies confirm ON. An interrupted sequence
resets its counter. The transition timestamp is the first observation in the confirming
sequence. Before confirmation, the previously confirmed state remains unchanged.
Local execution failures, unavailable ICMP tooling, and a hung subprocess produce
UNKNOWN/UNAVAILABLE rather than a confirmed outage. A missed packet can degrade monitor
health while power stays ON. Counters, reply status and fixed diagnostic reason codes
are safe to log; raw exception text and credentials are excluded.

The monitoring service persists state, health, check timestamps and interval history
in one transaction, then publishes domain events. Repeated states do not split intervals.
New devices retain explicit UNKNOWN history before the first confirmed state. No backend
imports Telegram or planned schedule code. The connection-test button uses a separate
probe, renders Ukrainian feedback, and does not modify actual power history.

On restart, the current confirmed state is loaded from PostgreSQL and any open interval
is reused. Pending confirmation counters restart conservatively; persist observation
windows if exact recovery of an unfinished sequence across process failure is required.
There is no schema change or new Python dependency for Ping scheduling. Local Ping needs
`iputils-ping` and ICMP permissions; the Docker image already installs it.

## Power notifications

Apply the new additive migration before starting the updated application:

```bash
alembic upgrade head
# Or:
docker compose run --rm app alembic upgrade head
```

With `BOT_ENABLED=true`, confirmed ON-to-OFF and OFF-to-ON events produce Ukrainian
outage/restoration messages. Times use Europe/Kyiv; durations come from contiguous
persisted power intervals in UTC. Shared `app.formatting` handles Ukrainian plural
forms and hours/minutes, including teen numbers and sub-minute intervals. Initial
UNKNOWN observations, transitions through UNKNOWN, and unchanged states do not produce
false restoration/outage messages or durations that include unknown time.

Delivery uses every enabled owner-scoped channel assigned to the device. Disabled or
soft-deleted devices are ignored. Messages explicitly disable parse mode so names
cannot inject Telegram markup. Sends have a ten-second timeout. One channel's Telegram
failure does not stop the others; a rejected rate-limited request gets one retry only
when Telegram's requested delay is at most 30 seconds.

Revision `0002` creates `power_notification_deliveries`: the unique key is the persisted
transition interval plus signed 64-bit Telegram chat ID. Reservations commit before
network delivery. Repeated event UUIDs, new UUIDs for the same transition, concurrent
subscribers, channel recreation, and application restarts cannot resend a reserved
notification. Status records distinguish claimed, sent, rejected, and uncertain sends.
Rows retain references to history deliberately; deleting a notification channel does
not erase deduplication records. The migration upgrade changes no existing records.
Downgrade drops delivery records (losing deduplication), while retaining power history.

This policy favors at-most-once delivery: an interrupted or ambiguous send can be lost,
and is not automatically retried. A crash before event dispatch can also miss an alert,
because the event bus remains in-process. Telegram has no idempotency key for sendMessage;
a durable outbox can recover unsent work but cannot eliminate the ambiguity of a send
whose response was lost. Claims are made per channel just before sending, so interruption
on one channel does not reserve all remaining channels prematurely.

## SNMP monitoring

`SnmpMonitor` implements `PowerMonitor` with bounded asynchronous SNMP v2c GET
requests using pysnmp 7. Interface mode queries `1.3.6.1.2.1.2.2.1.8.<index>`:
1 means ON, 2 means OFF, all other values remain UNKNOWN. Custom mode accepts
numeric OIDs and configured integer or string values. Decimal integer strings
compare numerically (including signs/leading zeros); other strings compare exactly,
case-sensitively. Numerically equivalent ON/OFF values are rejected.

Connection tests are available during setup and from saved device details. Numeric
ASN.1 values are read as integers; UTF-8 octet strings are compared as text.
Unexpected values produce UNKNOWN/DEGRADED. Timeouts, access errors, missing OIDs
and transport failures produce UNKNOWN/UNAVAILABLE with fixed safe diagnostic codes.
A wrong v2c community may cause a timeout rather than an explicit access error.
Neither raw response values nor upstream exception messages enter diagnostics.

Communities remain encrypted in the database/FSM and are never displayed after
entry. Set `ENCRYPTION_KEY` and install the declared `pysnmp` and `cryptography`
dependencies before using this backend. Polling intervals are validated and saved;
SNMP background scheduling is not yet implemented. Diagnostic checks do not alter
power interval history or send notifications.

## Home Assistant webhooks

`POST /api/v1/homeassistant/webhook/{token}` accepts only a JSON object containing
`state: "on"` or `state: "off"`. Valid updates return 204; invalid payloads return
422, missing/disabled/expired tokens return 404, rate-limited updates return 429
with `Retry-After: 60`, and unavailable Redis returns 503.

Setup reserves a cryptographically random 256-bit token in Redis using atomic NX
and a 30-minute expiration. Only its SHA-256 hash enters FSM/database storage;
the existing database unique constraint prevents duplicate active tokens. The
plaintext URL appears once, inside the Telegram example, and is not retrievable
from saved device details. Reconfiguring creates a replacement token. Access logs
redact webhook URLs; reverse proxies must also redact these paths.

The Redis limiter allows 30 requests per token per minute. Deploy an ingress request
size limit and IP rate limiter for broader protection against floods of invalid tokens.
The example sends state changes, startup state, and one heartbeat per minute, and
filters unknown/unavailable states. Actual history and events use locked, idempotent
transactions; duplicate states update check timestamps without creating intervals
or repeating state-change notifications. No additional migration is needed.

The example uses Home Assistant's documented
[REST command](https://www.home-assistant.io/integrations/rest_command/) and
[automation triggers](https://www.home-assistant.io/docs/automation/trigger/).

## Advanced Home Assistant integration

Choose **🌐 WebSocket (розширений)** during Home Assistant setup. Provide an HTTP(S)
base URL, long-lived access token, entity ID, and exact case-sensitive ON/OFF state
values. `unknown` and `unavailable` cannot be mapped to confirmed power states.
The connection test authenticates, establishes a `state_changed` subscription, and
verifies the configured entity's current state before saving. Existing `api` mode
configurations now use WebSocket subscriptions with default `on`/`off` mapping.

The existing monitoring leader supervises one independent task per enabled API
device. Connections authenticate at `/api/websocket`, subscribe before reading an
initial snapshot, and process only the configured entity. Snapshot timestamps
represent observations now rather than backdating history to old HA changes.
Disconnects record UNKNOWN/UNAVAILABLE; reconnections recover health and current
state. Retries back off from 1 to 60 seconds. Transport heartbeats detect dead
connections. Configuration changes, disable/delete, loss of leadership, and shutdown
cancel subscriptions and close sockets. No monitoring adapter calls Telegram.

Access tokens remain encrypted and excluded from configuration snapshots' repr.
Handshake redirects are rejected before authentication; transport exceptions and
frames are never logged. Diagnostic metadata contains only safe fixed codes.
Apply migration `0003` using `alembic upgrade head` before starting the new version.
Downgrade removes the mapping fields; default states become `on`/`off` again.

Protocol reference: [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/).

## Planned outage providers

The planned schedule subsystem is independent of monitoring devices and power history.
`ScheduleProvider` exposes region catalogs, outage groups/queues, and daily schedules.
`ScheduleService` returns normalized `ProviderResult` values containing data,
freshness (`fresh`, `cached`, `unchanged`, `stale`, or `unavailable`), download/check
UTC timestamps, and fixed safe error codes. Failed providers do not affect other
providers. Source JSON stays inside adapters; Telegram handlers need only domain models.

The included bulk adapter supports the reference project's legacy and unified proxy
formats, including JSON-string `body` envelopes. `SCHEDULE_SOURCES` configures independent
provider IDs/URLs; `{}` disables them. Region/group catalogs come from source data.
The adapter conservatively rejects malformed/empty catalogs or day maps, retains
last-good data, and treats missing half-hour labels and unknown integer codes as
UNKNOWN. Only `1` means planned ON and `2` means planned OFF. Emergency metadata
remains separate; schedules never confirm actual electricity availability.

`app.state.schedules` is ready for consumers after startup:

```python
result = await app.state.schedules.get_schedule("dtek", "kyiv", "1.1")
if result.data is not None:
    schedule = result.data  # provider, region, group, date, timezone, slots, emergency
    # Inspect result.freshness before presenting this as recently confirmed data.
```

Omitting the date uses today's Europe/Kyiv date. Slots contain aware UTC instants
and cover the complete Kyiv calendar day, coalescing adjacent equal states. The
normalizer walks the UTC timeline: spring DST days contain 46 half-hours, autumn
50. Nonexistent civil labels have no interval; repeated civil labels apply to both
folds when the upstream cannot distinguish them. Normal days have 48 half-hours.
Actual elapsed durations are calculated from UTC endpoints.

All users, groups, dates, and application processes share Redis entries keyed by
upstream URL. Refreshes use expiring distributed leases, a cache recheck after
acquisition, and atomic ownership-checked publication/release. Cache keys share a
Redis Cluster hash tag. HTTP requests have an overall deadline, bounded response
size, exponential retry delays, and no redirects. Supported validators are persisted
and sent as `If-None-Match` and `If-Modified-Since`; 304 updates the successful-check
time without changing the download time or schedule content.

Defaults: 600 seconds fresh, 8-second request timeout, two retries, 24-hour retention.
Failed refreshes cool down for 30 seconds; numeric long `Retry-After` values postpone
retry for up to 24 hours. Last-good data is explicitly STALE without advancing its
successful-check timestamp; exhausted retention yields UNAVAILABLE. Redis failure
never bypasses coordination to send independent upstream requests. Fetches are lazy,
and the shared HTTP client closes during application shutdown.

The adapter follows the currently observed reference schema and can be replaced as
external APIs change. Live proxy availability is not asserted by the mocked tests.
Background subscription refresh and schedule version persistence/change detection
remain separate feature tasks. The provider subsystem adds no database migration or dependencies.

Architectural references studied:
[shared source hub](https://github.com/chaichuk/svitlo_live/blob/main/custom_components/svitlo_live/api_hub.py)
and [half-hour normalization](https://github.com/chaichuk/svitlo_live/blob/main/custom_components/svitlo_live/coordinator.py).
No Home Assistant coordinator, entity, or timer architecture is used here.

## Schedule subscription management

Open **📍 Групи відключень** in a private chat. **➕ Додати групу** starts a guided
region → outage group → friendly name → notification channels flow. Catalogs are
provided dynamically by `ScheduleService`; handlers contain no regional catalogs
or provider-specific parsing. Multiple providers' regions appear in one selector;
duplicate region names receive a source label. Stale/partial catalogs are marked,
and a missing catalog produces a Ukrainian retry message rather than invented choices.
The selected group is revalidated against the provider before saving.

Existing subscriptions are grouped under their region names, including disabled
subscriptions. Details support rename, channel reassignment, explicit enable/disable,
and confirmed deletion. Channel selection is independent for each subscription;
zero channels is allowed. A private-chat notification channel is created idempotently
when channels are opened. Other Telegram channels must already belong to the user;
disabled channels are visibly marked and remain independently disabled. Registering
other chats belongs to the separate notification-channel management feature.

Selectors and subscription lists are paginated. Provider IDs and group identifiers
remain in Redis FSM data; callbacks use short indices and a flow nonce, avoiding
Telegram's 64-byte callback limit and rejecting stale keyboard selections. Save
replays use a persisted per-owner creation key, and channel/enable actions encode
explicit desired states. Application services enforce ownership and validate channel
assignments. Telegram handlers manage interaction state and rendering only.

Apply migration `0004` with `alembic upgrade head`. It adds a nullable creation key
and unique owner/key constraint; existing subscriptions remain unchanged. Deletion
removes only the selected subscription and its channel links. Notification channels,
shared schedule versions, and actual power history remain intact. Downgrade removes
the creation key and its replay protection. Schedule refresh/change notifications
are independent of this management UI.


## Schedule change notifications

With `BOT_ENABLED=true`, `ScheduleNotificationHandler` consumes `ScheduleChanged`
events referencing committed `ScheduleVersion` snapshots. `normalized_content` must
contain `DaySchedule.model_dump(mode="json")`; source identifiers and dates must
match the stored version and event. Missing/malformed snapshots and equivalent
intervals are ignored. Provider timestamps and arbitrary metadata do not determine
whether two versions differ. Schedule refresh/version creation and event production
are not implemented by this consumer.

Messages show Ukrainian dates and Europe/Kyiv intervals, use `24:00` for the end
of the day, and calculate total planned OFF duration from UTC endpoints. UNKNOWN
periods remain separate. The example ranges 06:00–09:30, 14:00–18:00, 22:30–24:00
total nine hours. Emergency status changes are included. Region names come from
one shared catalog lookup per event; provider unavailability uses a Ukrainian fallback.

Only enabled matching subscriptions and enabled assigned channels receive notices.
Duplicate assignments to a shared Telegram chat produce one message per version.
**🔎 Що змінилося?** compares coalesced OFF and UNKNOWN intervals, excluding unchanged
ranges and showing additions/removals/expansions/shortenings as **Було / Стало**.
Equivalent slot segmentation is ignored. Long diffs are split into Telegram-sized
messages. The button can only read versions reserved for the message's destination
chat, including after a subscription has been deleted.

Apply additive migration `0005` with `alembic upgrade head`. It creates a delivery
ledger keyed by version and signed 64-bit Telegram chat ID, retaining the previous
version used for that notice's diff. Reservations commit before network sends and
survive replay, concurrency, channel recreation, and restart. A Telegram failure
for one chat does not block the others. The existing bounded 429 retry is reused.
As with power notices, delivery is at-most-once: interrupted/uncertain sends are
not automatically retried, and the in-process bus has no durable outbox.
Referenced schedule versions cannot be deleted; subscription deletion does not erase
this ledger. Downgrade drops reservations and loses notification replay protection,
while retaining schedule versions.


## Actual power analytics

`app.state.analytics` exposes owner-scoped `AnalyticsService.get_statistics`. Calculations
read only `PowerInterval` records, never planned schedules, current device state, or
monitor health. Disabled devices can still be queried; deleted/foreign/missing devices
are rejected using the existing device repository. No migration or dependency is needed.

```python
from app.analytics.service import AnalyticsPeriod
from app.analytics.reports import format_statistics

statistics = await app.state.analytics.get_statistics(
    user_id, device_id, AnalyticsPeriod.CURRENT_MONTH
)
text = format_statistics(statistics, "Дім")
```

Supported periods: `TODAY`, `YESTERDAY`, `LAST_7_DAYS`, `CALENDAR_WEEK`, `CURRENT_MONTH`,
`PREVIOUS_MONTH`. Calendar boundaries use Europe/Kyiv; weeks begin on Monday, and
last seven days means today plus the preceding six calendar days. Current periods
stop at a single captured current instant; completed periods stop at their end.
UTC elapsed durations correctly account for DST, leap months and year transitions.
An explicit aware `now=` makes both service and pure calculations reproducible.

Statistics contain ON, OFF and UNKNOWN durations, known-time availability percentage,
outage count, longest outage and average outage duration. Availability is
`100 * ON / (ON + OFF)`, or `None` if no known time exists. Missing history before,
between or after records contributes to UNKNOWN. Future time is never included.
Open and cross-boundary intervals are clipped to the elapsed reporting window.

Outage count means contiguous OFF episodes overlapping the window, including outages
that started earlier or are ongoing. Longest/average use clipped durations inside the
same window, including ongoing episodes. Adjacent OFF rows are merged; UNKNOWN or gaps
split episodes. Consequently an outage crossing midnight can appear in both daily
reports, but counts once in a report covering both days. Invalid overlapping history
is rejected rather than double-counted. Calculations do not modify stored intervals.

`format_statistics` renders Ukrainian labels, Kyiv timestamps, UNKNOWN duration,
and an explicit known-time percentage denominator. The Telegram statistics menu and
automatic reports use this service and formatter.


## Telegram statistics menu

Open **📊 Статистика** in a private chat, choose a device, then choose **📅 Сьогодні**,
**↩️ Вчора**, **7️⃣ 7 днів**, **🗓 Тиждень**, or **📆 Місяць**. Device choices are paginated;
disabled devices retain access to their historical data. Back returns to periods or
devices, and cancel returns to the main menu. Opening statistics clears an unfinished
setup wizard. Old callbacks revalidate device ownership and fetch current history.

Thin handlers invoke the device and analytics application services. Telegram user IDs
are mapped to database user IDs inside the analytics service, never trusted as internal
IDs. Foreign/deleted devices, unsupported periods and dependency failures receive
Ukrainian errors. Private history is not shown in group/channel chats.

Results use Ukrainian date headings, Kyiv timestamps, compact `год`/`хв` durations,
and rounded ON/OFF percentages that sum to 100 over known time. UNKNOWN is displayed
explicitly when nonzero and is excluded from the percentage denominator. All-UNKNOWN
history shows no percentage rather than implying an outage. No migration is required.


## Automatic reports

Open **⚙️ Налаштування → 📊 Автоматичні звіти**, or use the automatic-report button
in statistics device selection. Choose a device and notification channel. Daily,
weekly and monthly flags are independent for every device/channel pair. Each has
its own editable Kyiv local time (default 09:00); weekly delivery also has an editable
weekday (default Monday). Monthly delivery occurs on the first day after the previous
calendar month ends. Disabled channels are marked and do not receive reports.

The selector lists existing owned channels and creates the user's private channel
idempotently. Selecting a channel explicitly links it to the device: the screen
explains that it receives device power notifications and enabled reports. Other
chats must already be registered. Report disablement changes only its flag; it does
not erase intervals, other report flags or delivery history.

Daily reports cover yesterday; weekly reports cover the last completed Monday–Sunday
week, regardless of which delivery weekday is selected; monthly reports cover the
previous completed month. Boundaries use Europe/Kyiv and stored instants use UTC.
At autumn DST repetition the first occurrence is used once; nonexistent spring times
roll forward by the DST gap. New activation cutoffs prevent enabling a report from
sending older already-due occurrences. Repeated enable actions preserve that cutoff.

`ReportWorker` runs independently of monitoring, whenever `BOT_ENABLED=true`, checks
for work every 30 seconds, limits concurrent preparation/sends to eight, and closes
on application shutdown before the Telegram client. On restart/downtime it recovers
only the latest due occurrence for each type, without flooding a channel with a
historical backlog. It rechecks ownership, enablement and schedule after rendering.
Database/rendering failures before reservation retry on later checks; one device or
channel failure does not suppress the rest.

Charts use Pillow in a background thread, show actual ON/OFF/UNKNOWN totals and daily
stacked breakdowns, label numeric durations, and hatch UNKNOWN for clarity beyond
color. Analytics reads only `PowerInterval` history; scheduled outages never enter
these calculations. A single Telegram photo carries the PNG and Ukrainian summary.
Unknown time remains explicit and availability uses only known time.

Apply migration `0006` with `alembic upgrade head`. Existing flags are retained;
new time fields default to 09:00 and the weekly weekday to Monday. It adds a durable
ledger keyed by device, Telegram chat, report type and UTC period end. Reservations
commit before network delivery, preventing duplicate sends across restarts, races,
settings changes and channel recreation. Delivery status distinguishes claimed,
sent, failed and uncertain. As with other notifications, interrupted/ambiguous sends
are at-most-once and are not automatically retried; a bounded explicit 429 rejection
gets one retry. Deleting channels/settings retains ledger and actual interval history.
Downgrade removes new schedule fields and the delivery ledger (losing replay protection),
while retaining report flags and power history.

Install updated dependencies with `python -m pip install -e '.[dev]'`. Docker includes
`Pillow` and `fonts-dejavu-core`; local development also needs DejaVu Sans installed
at `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` (Fedora's Hack-Regular path is
supported too). A missing Cyrillic font fails preparation before a delivery is claimed,
so the application cannot silently send unreadable Ukrainian charts.

## Notification channel management

Open **🔔 Канали** to register, name, inspect, enable/disable or delete destinations.
Private destinations are restricted to the requesting user's own chat. For a group,
supergroup or channel, add the bot as an administrator, then choose the destination
with Telegram's native chat picker. A public `@username` or negative numeric chat ID
can also be entered. Both the requesting user and the bot must be administrators;
channels additionally require the bot's posting permission. The service verifies
permissions again before saving and before re-enabling. Registration upserts the
owner/chat pair, preserving assignments when the same destination is registered again.

Choose **⚙️ Пристрої → device → 🔔 Канали → ⚙️ Обрати канали** to manage device
assignments. Schedule subscription channel selection uses the same owned catalog;
the two sets of assignments remain independent. Disabled channels retain assignments.
Removing a device assignment removes its automatic report settings. Confirmed channel
deletion removes all assignments and report settings, retaining power history and
notification delivery ledgers. Existing database constraints cover this feature;
no migration is required.

Power alerts, schedule alerts and report photos share live destination checks with
bounded Telegram request timeouts. Missing access or insufficient posting/media
permissions prevents sending; check failures fail closed. Explicit rate-limit retries
repeat the access check. Telegram can still reject delivery if permissions change
between checking and sending, or if a private recipient has blocked the bot; existing
notification handlers record those rejections without retrying ambiguous deliveries.
Network-facing tests use mocked Telegram responses.

## Monitor reliability and health warnings

Apply `alembic upgrade head` (migration `0007`) before starting the updated app.
Devices now retain a communication-failure streak, its first observation timestamp,
the latest processed observation and the latest health transition. These survive
restarts. Duplicate/out-of-order samples cannot advance the streak or overwrite
newer state. Reconfiguring a device clears the previous configuration's streak.

A communication/adapter failure first sets **DEGRADED**, retaining the previous
power state. After `MONITOR_FAILURE_THRESHOLD` distinct consecutive failed checks
(default 3), health becomes **UNAVAILABLE** and power becomes **UNKNOWN** from the
first failed observation. A response clears the failure streak. An unexpected
SNMP/HA state is UNKNOWN with DEGRADED health even though communication succeeded.
UNKNOWN intervals remain separate from confirmed outages in analytics and do not
produce ordinary power outage/restore alerts.

Ping target no-replies retain their existing independent failure/recovery debounce;
a single lost packet never produces OFF. Executing Ping unsuccessfully is instead a
communication/adapter failure. First recovery observations cannot create premature
power transitions before Ping confirmation. The monitoring leader now schedules
SNMP as well as Ping, alongside independent HA WebSocket tasks. Initialization
failures affect health, while database/event-consumer failures do not masquerade
as communication failures. Webhook devices become healthy when valid states arrive;
silence alone cannot establish a failure without an agreed heartbeat contract.

Health notifications are optional and disabled by default:

```dotenv
MONITOR_FAILURE_THRESHOLD=3
MONITOR_HEALTH_WARNINGS_ENABLED=true
MONITOR_HEALTH_RECOVERY_ENABLED=false
MONITOR_HEALTH_WARNING_COOLDOWN_SECONDS=3600
```

Warnings are separate `MonitorHealthChanged` consumers, sent only for UNAVAILABLE
to active assigned device channels, with the last successful check shown in Kyiv
time. DEGRADED alone stays quiet. Recovery messages require explicit configuration
and a successfully sent warning. Database reservations and per-device/chat cooldowns
suppress duplicate events, restart replays and rapid flapping, including channel
recreation. Old health events must match the currently persisted transition before
sending. One channel failure does not prevent delivery to others; live Telegram
access checks also apply. Event dispatch retains the existing in-process bus:
reservations prevent duplicate sends but do not provide a transactional event outbox.

Migration upgrade preserves existing power history and settings. Downgrade removes
failure-tracking fields and warning state, losing their restart/cooldown protection;
it leaves power intervals intact. Tests cover failure confirmation, restart/replay,
UNKNOWN analytics, optional recovery, flapping and migration upgrade/downgrade.

## Production deployment

### Host, image and secrets

Use a host with Docker and Docker Compose, durable storage, and a TLS reverse proxy.
Run **one application instance and one Uvicorn worker per bot token**, including
webhook traffic. Monitoring leadership is protected by a PostgreSQL advisory lock,
but the in-process event bus does not forward events between replicas. Adding
API-only replicas without an outbox can lose notifications even though database
writes remain protected. Build an image once per release and retain its digest
for rollback. Dependency ranges in `pyproject.toml` resolve during image building;
rebuilding later is not a reproducible release, so promote the built artifact.
`APP_IMAGE_TAG` sets the Compose image tag (default `local`).

Create `.env` from the example and restrict access (`chmod 600 .env`). Replace both
password placeholders with separate URL-safe random values. Set `BOT_ENABLED=true`,
`TELEGRAM_BOT_TOKEN`, a persistent Fernet `ENCRYPTION_KEY`, and a public HTTPS
`PUBLIC_BASE_URL`. Do not commit `.env`, print resolved Compose configuration into
logs, or rotate/discard the encryption key without re-encrypting saved configuration.
SNMP communities and HA access tokens in PostgreSQL are encrypted. Redis contains
transient FSM/setup data, which may include wizard secrets until expiry; protect its
volume, snapshots, network access and backups accordingly. Use encrypted host storage.

Ping, SNMP and HA API targets must be reachable from the application container;
private targets usually need VPN/private routing. Keep ICMP permissions narrowly
scoped rather than running a privileged container. The worker reports execution
permission failures as monitoring failures, not confirmed power outages.

### First deployment and upgrades

```bash
cp .env.example .env
chmod 600 .env
# Edit secrets, public HTTPS URL, flags and APP_IMAGE_TAG.
docker compose build app
docker compose up -d postgres redis
docker compose run --rm app alembic upgrade head
docker compose up -d app
curl --fail http://127.0.0.1:8000/readiness
docker compose logs --tail=100 app
```

Migration `0008` adds partial indexes for enabled monitoring targets and active report
settings; it does not remove data. Run migrations once before routing traffic. For
an existing deployment, back up PostgreSQL, stop the app, run the migration with the
new image, then start it and check readiness. Standard index creation can block writes;
plan a maintenance window for large databases. Do not run concurrent migration jobs.
Do not downgrade a database blindly to roll back code: inspect the migration first.

PostgreSQL and Redis volumes survive `docker compose down`. Do not use `down -v` on
production data. App logs are JSON; route stdout/stderr to the host log collector and
set retention there. Keep dependency and image security updates under a tested release
process rather than rebuilding production containers opportunistically.

### TLS, access logs and abuse protection

Compose exposes application/database/Redis ports only on loopback. Expose only the
HTTPS webhook route through the proxy; keep health/readiness and database/cache access
internal. Webhook URLs are credentials: **disable access logging for that route** at
the proxy and any CDN, including error diagnostics that might include the request URI.
Never log request bodies or authorization headers.

Example Nginx directives (install your own certificate and replace the hostname):

```nginx
# In the http context:
limit_req_zone $binary_remote_addr zone=svitlo_webhooks:10m rate=2r/s;

server {
    listen 443 ssl;
    server_name svitlo.example.com;
    ssl_certificate /etc/ssl/svitlo/fullchain.pem;
    ssl_certificate_key /etc/ssl/svitlo/privkey.pem;

    location /api/v1/homeassistant/webhook/ {
        access_log off;
        client_max_body_size 4k;
        client_body_timeout 10s;
        limit_req zone=svitlo_webhooks burst=10 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 60s;
    }
    location / { return 404; }
}
```

Configure the proxy's error logs to avoid recording webhook request paths as well;
`access_log off` alone does not sanitize proxy error logs. Tune per-IP limits for
multiple devices behind the same NAT. The app independently limits tokens to 30
updates/minute, rejects webhook bodies over 4 KiB including chunked uploads, and
limits body read time to 10 seconds. Redis failure makes webhook rate limiting fail
closed with 503. Validation and database errors return fixed responses without
submitted secrets. Uvicorn limits concurrent requests to 128; do not trust forwarded
headers from arbitrary peers.

### Readiness, recovery and shutdown

`/health` returns 200 for a live process, with bounded parallel database/Redis probes.
Dependency failures are reported as `degraded` with no connection strings or error
messages. `/readiness` additionally returns 503 if dependencies fail, shutdown has
started, or a configured background task has stopped. Its `workers` map is omitted
when no workers are configured. Neither endpoint proves upstream device/provider
reachability or that a migration has been applied: verify `alembic current` separately.
Dockerfile and Compose app health checks use `/readiness`; PostgreSQL and Redis also
have health checks. Docker marks an unhealthy container but does not automatically
restart it solely because a health check failed; alert on readiness failures.

Database connections use pre-ping, bounded pool waits, 30-second query/statement
limits and 10-second lock waits. Redis reconnects on subsequent operations, with
idle connection health checks; ambiguous mutating commands are not automatically
replayed. Schedule HTTP fetches have bounded timeouts, conditional headers, jittered
exponential retry, numeric/date `Retry-After` handling and shared Redis leases.
Provider failure preserves last-known-good data as stale and never overwrites valid
versions. A shared schedule worker polls distinct enabled sources for today/tomorrow,
stores canonical content hashes under transaction-scoped PostgreSQL locks, and emits
change events after commit. The initial version establishes a baseline without
sending a change warning.

Unexpected polling failures restart with capped backoff. ICMP/SNMP/HA tasks are
isolated; failed tasks are rediscovered and restarted without stopping other devices.
Telegram UI and notification API calls retry a bounded explicit flood rejection once
(up to 30 seconds), with one retry owner so retries do not multiply. Network errors
with ambiguous delivery are not retried. Notification destinations/assignments are
rechecked before reservation, and bot permissions are checked before sending.

SIGTERM stops accepting work, stops polling through aiogram's native stop signal,
and cancels active Telegram handler tasks before closing shared clients. Workers
have a `SHUTDOWN_TIMEOUT` cancellation deadline (default 10 seconds each). Compose
allows 60 seconds before forced termination and uses an init process for child reaping.
Redis is owned and closed once by application lifespan rather than by FSM storage.

### Backups, validation and limits

Back up PostgreSQL and retain the encryption key separately with equivalent access
controls. For example, on the deployment host:

```bash
mkdir -p backups
chmod 700 backups
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backups/svitlo.sql
chmod 600 backups/svitlo.sql
```

Test restoration on an isolated stack before relying on a backup. Redis AOF keeps
FSM/cache state across restarts; loss of Redis should not erase PostgreSQL power
history, but can invalidate active setup flows and shared cache. Avoid restoring old
notification ledgers independently of their matching history.

Run release checks in the development environment:

```bash
python -m pytest
ruff check .
mypy .
```

Tests mock external monitoring/Telegram/providers. Migration tests check real SQLite
upgrade/downgrade/schema parity and generated PostgreSQL SQL; live PostgreSQL locks,
real device connectivity and container builds need staging verification. Event dispatch
remains in-process, with durable at-most-once notification reservations. A crash between
a state/version commit and publication can lose an alert; guaranteed eventual delivery
requires a transactional outbox. Uncertain deliveries are deliberately not resent.
Some main-menu entries still show the Ukrainian development placeholder; a healthy
backend is not a claim that every product menu is complete.
