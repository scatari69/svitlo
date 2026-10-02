# Architecture review

## Components and ownership

| Component | Responsibility |
| --- | --- |
| `app/main.py`, `app/config.py` | Dependency composition, environment configuration, worker supervision and graceful lifecycle |
| `app/bot/` | Ukrainian presentation, input parsing and FSM navigation; application services own business logic |
| `app/services/`, `app/repositories/` | Owner-scoped operations, transactional persistence and shared validation |
| `app/monitoring/`, `app/workers/` | Independent asynchronous adapters, result normalization, confirmation and scheduling |
| `app/events/`, `app/notifications/` | Domain dispatch, safe logs and channel-aware Telegram delivery |
| `app/schedules/` | Provider catalogs, shared fetching, normalized planned schedules and meaningful diffs |
| `app/analytics/` | Actual interval statistics, report scheduling, Ukrainian formatting and Pillow charts |
| `app/models/`, `app/db/`, `alembic/` | Constraints, UTC timestamps, async sessions and versioned schema |
| `app/api/` | Health/readiness and validated, rate-limited Home Assistant webhooks |

## Flows

### Monitoring and events

Ping and SNMP implement `PowerMonitor.check()`. Home Assistant provides webhook
observations and an independent WebSocket stream for each configured entity.
Adapters produce normalized ON/OFF/UNKNOWN states and separate monitor health;
they contain neither Telegram delivery nor planned schedule logic.

The PostgreSQL advisory-lock leader supervises polling and WebSocket workers.
Network checks have bounded concurrency and timeouts. Ping confirms failure and
recovery streaks; persistent transport errors become UNKNOWN rather than false
outages. Detection time retains the first observation in a confirming sequence.

`MonitoringService` locks the device, rejects stale observations and obsolete
configurations, and applies failure tracking. Both polling and webhook paths use
`services/power.py` to persist history and health inside their transaction. The
interval repository closes the previous interval before inserting its successor;
a partial unique index prevents two open intervals for one device.

After commit, the in-process event bus dispatches power/health events to history,
logging and notification consumers. History replay is idempotent. Notification
consumers check channel assignments and reserve durable delivery keys before
sending. UNKNOWN transitions do not produce normal outage/restoration alerts.
Health warnings have persistent deduplication and cooldown state.

### Planned schedules

Enabled subscriptions are reduced to distinct provider/region/group sources.
Refreshes process today and tomorrow in batches of eight. Providers reuse a
Redis cache and atomic source lease: many subscriptions share upstream HTTP data.
HTTP requests have response limits, timeouts, bounded backoff and conditional
ETag/Last-Modified headers. Invalid data retains the last known good schedule with
explicit stale/unavailable metadata.

Normalized complete Kyiv days are hashed by content, merging equivalent adjacent
slots. A PostgreSQL transaction advisory lock serializes version persistence per
source/day. Only changed content creates a change event; notification reservations
are unique per version/chat. Telegram differences describe intervals, not JSON.
This subsystem never changes actual device power state.

### Analytics and reports

Analytics clips actual `PowerInterval` records to UTC bounds derived from Kyiv
calendar periods. Open intervals stop at the observation time; gaps and UNKNOWN
intervals remain unknown. Availability divides ON time by ON + OFF time only.
Schedules do not contribute to actual availability or outage counts.

Daily, weekly and monthly settings belong to a device/channel pair. Report
refreshes queue at most eight deliveries at once. Device locks precede report
settings locks, matching assignment edits. Configuration is checked again before
reservation; chart rendering runs outside database transactions and off the event
loop. Disabling reports retains actual history.

## Refactoring completed

- Centralized transactional observation persistence for polling and webhooks.
- Diagnostic connection tests preserve worker health, failure streaks and stale
  observation guards; successful diagnostics cannot regress the last-success time.
- Unified device-first locking for report settings and delivery versus channel edits.
- Bounded report/schedule queued tasks rather than allocating a task per target.
- Released polling capacity before persistence/event consumers; discovery failure
  preserves healthy child workers while leadership loss still stops them.
- Blocked the device-list entry point in group chats to protect private device data.
- Moved generic host/URL validation out of monitoring into shared validation.
- Rejected naive timestamps in device, health and schedule presentation boundaries.
- Added architecture-boundary and behavior regression tests. No schema migration or
  new dependency was needed.

Review also checked existing indexes, open-interval uniqueness, owner-scoped
foreign keys, schedule version/delivery deduplication, async network transports,
secret-safe logging and Ukrainian Telegram presentation. Technical protocol/state
names and user-supplied names may contain Latin characters.

## Remaining technical debt and operational limits

- The event bus is in-process. A crash after commit and before dispatch can lose a
  notification. Durable reservations favor avoiding duplicates over retrying an
  ambiguous Telegram send. A transactional outbox is needed for recoverable event
  delivery and safe notification-producing replicas; deploy one application
  instance under the current deployment contract.
- Polling keeps one lightweight task per device; WebSocket mode keeps one connection
  per configured device. Discovery still loads target snapshots into memory. At
  larger deployments, measure these costs before adding paginated discovery or
  shared Home Assistant subscriptions.
- Report recovery sends the latest due completed period, without backfilling every
  missed report. Ping confirmation streaks restart after a process restart, while
  persisted state, intervals and communication-failure tracking survive.
- Automated tests use mocked transports and SQLite-backed repository fixtures.
  PostgreSQL row-lock behavior, live provider contracts and container deployment
  need integration/staging verification. Docker is unavailable in this workspace.
- The main-menu actual-status and planned-schedule browsing actions remain existing
  placeholders; this review did not introduce new product features.
