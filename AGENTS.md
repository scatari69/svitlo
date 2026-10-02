# AGENTS.md

## Project overview

This repository contains a production-oriented Telegram bot for monitoring electricity availability in Ukraine.

The application has two independent data domains:

1. **Actual electricity state**
   - Home Assistant
   - ICMP Ping
   - SNMP
   - future monitoring backends

2. **Planned outage schedules**
   - Ukrainian regional outage schedules
   - outage groups / queues
   - schedule change detection
   - schedule notifications

These two domains MUST remain separate.

Planned schedules describe what is expected to happen.

Monitoring devices describe what actually happened.

Analytics may compare planned and actual data, but monitoring logic must never depend on planned schedules.

---

# Language rules

## User-facing language

ALL Telegram-facing text MUST be written in Ukrainian.

This includes:

- menus
- inline buttons
- setup wizards
- notifications
- validation errors
- help messages
- statistics
- report text
- warnings
- confirmation dialogs
- fallback messages

Technical product names may remain untranslated when appropriate:

- Home Assistant
- SNMP
- Ping
- Redis
- PostgreSQL

Do not introduce English user-facing strings accidentally.

## Internal language

Use English for:

- source code
- variable names
- function names
- classes
- comments
- docstrings
- logs
- tests
- documentation for developers
- API schemas
- database names
- commit messages

---

# Technology stack

Use:

- Python 3.13+
- aiogram 3
- FastAPI
- PostgreSQL
- SQLAlchemy 2 async
- Alembic
- Redis
- httpx
- asyncio
- APScheduler or an equivalent lightweight async scheduler
- pysnmp
- pytest
- Ruff
- mypy
- Docker
- Docker Compose

Prefer asynchronous implementations throughout the application.

Do NOT introduce Celery unless there is a demonstrated technical requirement.

---

# General development rules

Before changing code:

1. Inspect the existing implementation.
2. Understand the current abstractions.
3. Reuse existing services where appropriate.
4. Avoid duplicating logic.
5. Check existing migrations before modifying database models.
6. Check relevant tests before implementing changes.

After changing code:

1. Run relevant tests.
2. Run the complete test suite when practical.
3. Run Ruff.
4. Run mypy.
5. Fix meaningful failures before finishing.
6. Summarize important architectural or behavioral changes.

Do not leave knowingly broken tests or type errors without explicitly explaining why.

---

# Architecture principles

Use clear separation of concerns.

Preferred high-level structure:

```text
app/
    main.py
    config.py

    bot/
        handlers/
        keyboards/
        states/
        middlewares/

    monitoring/
        base.py
        service.py
        ping.py
        snmp.py
        homeassistant.py

    schedules/
        base.py
        service.py
        normalizer.py
        models.py
        providers/

    analytics/
        service.py
        reports.py

    notifications/
        service.py
        telegram.py

    events/
        bus.py
        models.py

    api/
        homeassistant.py

    models/
    repositories/
    services/
    workers/

    db/
        session.py

tests/
```

The exact structure may evolve, but preserve modular boundaries.

---

# Telegram handler rules

Telegram handlers MUST remain thin.

Handlers may:

- parse Telegram input
- validate basic interaction state
- invoke application services
- render responses
- transition FSM state

Handlers MUST NOT contain:

- monitoring logic
- schedule normalization
- analytics calculations
- direct SQL queries when a repository/service exists
- schedule hashing
- outage duration calculations
- SNMP parsing logic
- HTTP provider logic

Business logic belongs in services/domain modules.

---

# Domain event architecture

Monitoring implementations MUST NOT send Telegram notifications directly.

Monitoring backends produce normalized domain events.

Use concepts similar to:

```python
class PowerState(Enum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"
```

Example event:

```python
PowerStateChanged(
    device_id=device_id,
    previous_state=PowerState.ON,
    new_state=PowerState.OFF,
    detected_at=timestamp,
)
```

Possible event consumers:

- power history
- analytics
- Telegram notifications
- logging
- future integrations

Keep event producers independent from event consumers.

---

# Monitoring abstraction

All monitoring backends MUST return a normalized result.

Use a shared abstraction conceptually similar to:

```python
class PowerMonitor(Protocol):
    async def check(self) -> MonitorResult: ...
```

A monitor result should contain:

- normalized power state
- monitor health
- detection timestamp
- safe diagnostic metadata

Monitoring implementations must not expose transport-specific behavior outside their module.

---

# Power state

Use exactly three logical power states:

```text
ON
OFF
UNKNOWN
```

UNKNOWN MUST NOT be silently treated as OFF.

Examples of UNKNOWN:

- monitoring source unreachable
- invalid upstream state
- temporary provider failure
- unexpected SNMP value
- Home Assistant unavailable

---

# Monitor health

Power state and monitor health are separate concepts.

Use explicit health states such as:

```text
HEALTHY
DEGRADED
UNAVAILABLE
```

Examples:

- power may be ON while monitoring becomes DEGRADED
- a target being unreachable does not always prove power is OFF
- communication failure may eventually produce UNKNOWN

Do not conflate communication failure with confirmed outage.

---

# Devices

A user may own multiple monitoring devices.

Examples:

```text
Дім
Офіс
Дача
Батьки
```

Each device should support:

- name
- monitoring backend
- enabled state
- current power state
- monitor health
- last successful check
- last state change
- backend-specific configuration

Monitoring backends must be extensible.

Future backends should be addable without rewriting analytics or notifications.

---

# Ping monitoring

Ping monitoring configuration should support:

- hostname or IP
- interval
- timeout
- failure threshold
- recovery threshold

Recommended defaults:

```text
interval = 10 seconds
timeout = 2 seconds
failure threshold = 3
recovery threshold = 2
```

Never mark a device OFF after one failed packet.

Use debounce/state confirmation.

Example:

```text
ON
fail
fail
fail
=> confirmed OFF
```

Recovery example:

```text
OFF
success
success
=> confirmed ON
```

Where possible, preserve the timestamp of the first observation in the confirming sequence.

Avoid shell injection.

Prefer safe/native ICMP handling or carefully controlled subprocess execution.

---

# SNMP monitoring

Initially support SNMP v2c.

Configuration:

- host
- port
- community
- polling interval

Support two monitoring modes.

## Interface mode

Use IF-MIB `ifOperStatus`.

Map:

```text
1 -> ON
2 -> OFF
```

Other values should generally map to UNKNOWN unless explicitly configured.

## Custom OID mode

Support:

- OID
- expected ON value
- expected OFF value

Allow safe string or integer comparisons.

Unexpected values must not automatically become OFF.

Never log SNMP communities.

Never display stored communities in normal Telegram menus.

---

# Home Assistant integration

Support two integration modes.

## Webhook mode

This is the preferred simple integration.

Each device receives a secure unique webhook token.

Example endpoint:

```text
POST /api/v1/homeassistant/webhook/{token}
```

Normalized payload example:

```json
{
  "state": "on"
}
```

Supported normalized values:

```text
on
off
```

Map internally to PowerState.

Requirements:

- cryptographically secure webhook token
- request validation
- rate limiting / abuse protection
- no webhook secret in logs

## WebSocket/API mode

Advanced integration.

Configuration:

- Home Assistant URL
- long-lived access token
- entity_id

Subscribe to Home Assistant `state_changed`.

Requirements:

- automatic reconnect
- exponential backoff
- one failed HA instance must not affect others
- secrets never logged
- normalized PowerState output

---

# Secret handling

Never log or expose:

- Telegram bot token
- Home Assistant token
- SNMP community
- webhook secret
- database password
- Redis password
- encryption keys

Application-level secrets must come from environment variables or another appropriate secret-management mechanism.

Sensitive configuration should be encrypted at rest where feasible, or stored through an abstraction that allows encryption to be added safely.

Avoid printing full configuration objects.

---

# Power history

Do NOT use aggregate counters as the primary source of truth.

Store state intervals/events.

Example:

```text
ON
12:03 -> 14:46

OFF
14:46 -> 17:12

ON
17:12 -> ...
```

This history must allow recomputing:

- daily statistics
- weekly statistics
- monthly statistics
- future custom periods

State transitions MUST be idempotent.

Examples:

```text
ON -> ON
```

must not create a new interval.

```text
OFF -> OFF
```

must not create a new interval.

Only meaningful transitions should modify interval history.

Avoid multiple open intervals for the same device.

---

# Time handling

Store timestamps in UTC.

Use timezone-aware datetimes only.

Never introduce naive datetimes into domain logic.

Display user-facing Ukrainian times using:

```text
Europe/Kyiv
```

Correctly handle:

- DST
- midnight boundaries
- week boundaries
- month boundaries
- intervals crossing midnight

---

# Notification channels

Devices MUST NOT be tied directly to one Telegram chat.

Use a separate notification-channel model.

Support:

- private chat
- group
- supergroup
- Telegram channel where bot permissions allow posting

Use many-to-many relationships.

A device may notify multiple channels.

A channel may receive notifications from multiple devices.

Schedule subscriptions also use independently configurable channels.

---

# Power notifications

Consume PowerStateChanged events.

Do not send from monitoring backends.

Example outage message:

```text
🔴 Зникло світло

🏠 Дім
🕒 14:36

Світло було:
3 години 42 хвилини
```

Example restore message:

```text
🟢 Світло з’явилося

🏠 Дім
🕒 17:12

Світла не було:
2 години 36 хвилин
```

Use correct Ukrainian plural forms.

Avoid duplicate notifications.

---

# Ukrainian duration formatting

Implement reusable duration formatting.

Correct examples:

```text
1 хвилина
2 хвилини
5 хвилин

1 година
2 години
5 годин

1 година 1 хвилина
3 години 21 хвилина
```

Do not duplicate pluralization logic across handlers/services.

Keep it in a shared utility or presentation formatter.

---

# Telegram menus

Primary UI should be menu-driven.

Main menu:

```text
💡 Стан
📅 Графік відключень
📊 Статистика
⚙️ Пристрої
🔔 Канали
📍 Групи відключень
⚙️ Налаштування
```

Prefer inline keyboards and guided setup flows.

Use aiogram FSM for multi-step workflows.

Always provide clear navigation:

```text
⬅️ Назад
❌ Скасувати
```

Avoid requiring users to memorize commands.

---

# Device setup

New device flow starts with:

```text
Оберіть спосіб визначення наявності світла:
```

Options:

```text
🏠 Home Assistant
🌐 Ping
📡 SNMP
```

All backend setup flows should include connection testing before final save.

Example:

```text
🧪 Перевірити підключення
```

Never display secrets after they are stored.

---

# Planned outage schedules

The planned outage subsystem must be provider-based.

Use an abstraction conceptually similar to:

```python
class ScheduleProvider(Protocol):
    async def get_regions(self) -> list[Region]: ...

    async def get_queues(self, region: str) -> list[Queue]: ...

    async def get_schedule(
        self,
        region: str,
        queue: str,
    ) -> Schedule: ...
```

Provider-specific JSON must not leak into Telegram handlers.

Normalize all schedule sources.

---

# Schedule inspiration

Use the open-source project below as architectural reference where useful:

```text
https://github.com/chaichuk/svitlo_live
```

Relevant concepts worth adopting:

- unified region catalog
- provider normalization
- shared source cache
- schedule history
- 48 half-hour slot representation where useful
- HTTP retries
- ETag caching
- Last-Modified caching
- Europe/Kyiv handling
- content-based schedule change detection

Do NOT tightly couple this application to Home Assistant-specific code from that project.

Do NOT assume its current external APIs are permanently stable.

Keep provider implementations replaceable.

---

# Schedule normalization

All provider schedules must be converted into one normalized format.

Conceptually:

```json
{
  "date": "2026-10-02",
  "timezone": "Europe/Kyiv",
  "slots": [
    {
      "start": "00:00",
      "end": "02:00",
      "state": "on"
    },
    {
      "start": "02:00",
      "end": "05:00",
      "state": "off"
    }
  ]
}
```

Normalized schedule states:

```text
ON
OFF
UNKNOWN
```

Preserve provider/source metadata separately when useful.

---

# Schedule caching

External schedule APIs must be shared across users.

Never create one external request per subscription/user if multiple users need the same underlying data.

Example:

```text
500 users
Kyiv region
Group 1.2
```

should reuse common provider/cache data.

Use Redis and/or application caching.

Use conditional HTTP requests where supported:

```text
ETag
If-None-Match
Last-Modified
If-Modified-Since
```

Use sensible request timeouts and retry with exponential backoff.

Do not hammer provider infrastructure.

---

# Schedule subscriptions

Users may subscribe to multiple outage groups.

Example:

```text
Київська область

🏠 Дім — група 1.2
👪 Батьки — група 3.1
```

A schedule subscription should contain:

- provider
- region
- queue/group
- friendly name
- enabled state
- assigned notification channels

Do not hardcode catalogs in Telegram handlers.

---

# Schedule change detection

Do NOT trigger schedule notifications based only on an upstream `updated_at` field.

Normalize schedule content first.

Generate a deterministic canonical representation.

Hash using SHA-256.

Equivalent schedules must produce the same hash regardless of:

- JSON key ordering
- irrelevant metadata
- upstream update timestamp

Only emit ScheduleChanged when normalized content changes.

Store previous schedule versions.

---

# Schedule diff

Schedule differences must be human-readable.

Do not show raw JSON diff.

Example:

```text
Було:
14:00–17:00

Стало:
14:00–18:00
```

Support:

- added outage
- removed outage
- expanded outage
- shortened outage
- changed unknown periods

Keep diff logic in schedule/domain services.

---

# Schedule notifications

Example:

```text
⚠️ Графік відключень змінено

📍 Київська область
Група 1.2

Сьогодні, 2 жовтня

🔻 06:00 — 09:30
🔻 14:00 — 18:00
🔻 22:30 — 24:00

Загалом без світла:
10 годин
```

Provide:

```text
🔎 Що змінилося?
```

Respect configured notification channels.

Prevent duplicates.

---

# Schedule image rendering

Schedule image generation must be implemented as a separate service.

Prefer Pillow.

Generated image should include:

- date
- Ukrainian weekday
- region
- group
- 24-hour timeline
- ON state
- OFF state
- UNKNOWN state
- time labels

Do not rely only on colors.

Use labels, patterns, symbols, or other accessible visual distinction.

Rendering code must not depend on Telegram handlers.

---

# Analytics

Analytics must be calculated from actual PowerInterval data.

Never use planned schedules as actual electricity availability.

Support:

- today
- yesterday
- last 7 days
- calendar week
- current month
- previous month

Calculate:

- ON duration
- OFF duration
- UNKNOWN duration
- availability percentage
- outage count
- longest outage
- average outage duration

Handle partial/open intervals correctly.

---

# Availability calculation

UNKNOWN time must remain explicitly unknown.

Do not automatically include UNKNOWN in OFF duration.

If availability percentage is based only on known time, make that behavior explicit and consistent.

Example concept:

```text
known_time = ON + OFF
availability = ON / known_time
```

Do not silently pretend UNKNOWN never occurred.

---

# Reports

Support optional:

- daily reports
- weekly reports
- monthly reports

Report configuration should be scoped to:

```text
device + notification channel
```

Users must be able to independently enable or disable each report type.

Do not delete historical monitoring data when reports are disabled.

---

# Statistics example

Example Ukrainian output:

```text
📊 Статистика за 2 жовтня

💡 Світло було:
17 год 32 хв
73%

🔴 Світла не було:
6 год 28 хв
27%

Кількість відключень:
3

Найдовше відключення:
3 год 25 хв
```

If unknown data exists:

```text
⚪ Немає даних:
42 хв
```

---

# Plan versus actual

Future analytics may compare:

```text
planned:
14:00–18:00

actual:
14:17–17:42
```

Architecture and database design should preserve the ability to calculate:

- start deviation
- restoration deviation
- planned duration
- actual duration

Do NOT force this logic into the MVP unless requested.

Do not fabricate a match when actual and planned outages cannot be reliably correlated.

---

# Database rules

Use async SQLAlchemy 2.

Use Alembic for every schema change.

Never modify an already-applied migration just to make current code easier unless the project is explicitly still in disposable pre-production state.

Prefer a new migration.

Use:

- proper foreign keys
- explicit cascade behavior
- appropriate indexes
- unique constraints where needed

Telegram IDs must use 64-bit-compatible integer fields.

Use timezone-aware database timestamps.

Keep secret fields out of default repr/log output.

---

# Repository/service rules

Prefer repositories or focused data-access services over scattered SQLAlchemy queries.

Avoid repository abstractions that merely wrap every single ORM call with no benefit.

Use abstractions where they improve:

- transaction handling
- reuse
- testability
- domain separation

Do not leak SQLAlchemy session management into Telegram handlers.

---

# Concurrency

Be careful with:

- simultaneous monitoring results
- duplicated webhooks
- duplicated Telegram updates
- multiple workers
- schedule refresh races
- duplicate interval creation
- duplicate schedule version creation

Use:

- transactions
- locking where appropriate
- unique constraints
- idempotency

Do not rely solely on in-memory state for correctness when multiple processes may exist.

---

# Reliability

One broken device or provider must not crash the whole service.

Implement appropriate:

- retries
- timeouts
- backoff
- exception isolation
- reconnect logic
- graceful shutdown
- database connection handling
- Redis reconnect handling

External failures should degrade gracefully.

---

# Logging

Use structured logging.

Logs should make it possible to identify:

- device
- monitor backend
- provider
- schedule subscription
- worker
- event type

without exposing secrets.

Do not log massive provider payloads by default.

Use appropriate log levels.

---

# FastAPI

Keep FastAPI endpoints thin.

Endpoints should:

- validate input
- authenticate/authorize where required
- invoke services
- return normalized responses

Do not place substantial business logic inside route functions.

Provide at least:

```text
/health
/readiness
```

Health endpoints must not expose credentials or internal secrets.

---

# Docker

Provide production-friendly containerization.

Docker Compose should include at minimum:

- application
- PostgreSQL
- Redis

Requirements:

- health checks
- persistent database storage
- `.env.example`
- non-root app container where practical
- clean shutdown
- no credentials committed to repository

---

# Testing

Use pytest.

Network-facing tests should mock external systems unless explicitly testing integration behavior.

Important unit test coverage includes:

- power-state transitions
- duplicate transitions
- interval handling
- ping debounce
- ping recovery
- SNMP value mapping
- Home Assistant state mapping
- Ukrainian pluralization
- Ukrainian duration formatting
- schedule normalization
- schedule hashing
- schedule change detection
- schedule diff
- analytics across midnight
- weekly analytics
- monthly analytics
- UNKNOWN state behavior

Add regression tests when fixing bugs.

---

# Static checks

Expected project checks:

```bash
pytest
ruff check .
mypy .
```

Use the repository's actual configured commands if they differ.

Do not disable rules globally just to silence legitimate problems.

Prefer fixing code.

---

# Dependency rules

Before adding a dependency:

1. Check whether existing dependencies already solve the problem.
2. Prefer maintained libraries.
3. Avoid large frameworks for trivial tasks.
4. Keep dependency count reasonable.
5. Update project dependency files consistently.

Do not introduce a library merely to avoid writing a few simple lines of clear code.

---

# Migration rules

When database models change:

1. Inspect current model.
2. Inspect migration history.
3. Create a new migration.
4. Review generated SQL.
5. Ensure upgrade works.
6. Ensure downgrade is sensible where practical.
7. Update relevant tests.

Do not silently make destructive migrations.

Call out destructive changes explicitly.

---

# Refactoring rules

Refactor when it clearly improves:

- correctness
- reuse
- testability
- separation of concerns
- maintainability

Avoid speculative abstractions.

Do not turn simple logic into unnecessary design-pattern layers.

Prefer explicit, readable Python.

---

# Code quality

Prefer:

- small focused modules
- typed public interfaces
- clear naming
- deterministic behavior
- pure functions for calculations where practical
- explicit error handling

Avoid:

- giant service classes
- giant Telegram handlers
- global mutable state
- hidden side effects
- sync network calls inside async paths
- duplicated formatting logic
- duplicated database access logic

---

# User deletion and destructive actions

Destructive Telegram actions must require confirmation.

Example:

```text
Видалити пристрій «Дім»?

⚠️ Історія моніторингу може бути втрачена.
```

Buttons:

```text
🗑 Так, видалити
❌ Скасувати
```

Prefer soft delete or deliberate retention rules if historical analytics should remain available.

---

# Error messages

User-visible errors must be understandable and in Ukrainian.

Bad:

```text
aiohttp.ClientConnectorError
```

Good:

```text
❌ Не вдалося підключитися до пристрою.

Перевірте адресу та мережеве з’єднання.
```

Internal details belong in logs.

---

# External API behavior

Treat third-party APIs as unreliable.

Always assume they may:

- time out
- return invalid JSON
- change optional fields
- return HTTP errors
- temporarily return empty data

Validate responses.

Do not allow malformed provider data to corrupt previously valid schedule state.

Prefer retaining last known good schedule with appropriate stale metadata.

---

# Schedule freshness

Keep metadata distinguishing:

- fetched successfully
- unchanged
- stale
- provider unavailable

Do not falsely present stale schedules as newly confirmed data.

---

# Security review checklist

Before completing security-sensitive work verify:

- no secrets in logs
- no secrets in Telegram messages after initial setup
- webhook tokens are sufficiently random
- URLs are validated
- unsafe redirects are avoided
- user-provided hostnames/IPs are validated appropriately
- external HTTP requests have timeouts
- database queries are parameterized through ORM/query builder
- no shell injection path exists
- Telegram user access is scoped correctly

---

# Codex task workflow

For every requested task:

1. Read this `AGENTS.md`.
2. Inspect relevant repository files before editing.
3. Identify existing abstractions that should be reused.
4. Implement the requested behavior completely.
5. Update or create tests.
6. Run relevant test/lint/type-check commands.
7. Fix failures caused by the change.
8. Do not modify unrelated behavior without a clear reason.
9. Report what changed and any remaining limitations.

Do not only describe how to implement a requested feature if repository edits are possible.

Implement it.

---

# Completion report

At the end of each task, provide a concise report containing:

- implemented behavior
- important files changed
- migrations created, if any
- tests added/updated
- checks executed
- important limitations or follow-up work

Do not produce an excessively long summary.

---

# Architectural invariants

The following rules are especially important and MUST be preserved:

1. Telegram UI is Ukrainian.
2. Internal code and developer-facing text are English.
3. Telegram handlers remain thin.
4. Monitoring backends never send Telegram messages directly.
5. All monitoring backends normalize to ON/OFF/UNKNOWN.
6. Monitor health is separate from power state.
7. UNKNOWN is never silently treated as OFF.
8. Actual monitoring and planned schedules remain separate subsystems.
9. Power history stores intervals/events rather than only aggregates.
10. External schedule data is shared/cached across users.
11. Schedule changes are detected from normalized content, not timestamps.
12. Secrets never appear in logs.
13. Datetimes are timezone-aware.
14. Database schema changes use Alembic.
15. New features include appropriate tests.
