# hotel-mvp-backend

Backend of a hotel booking system where rooms are sold in **30-minute blocks**. It prevents
double bookings under heavy concurrency, pushes every change to connected browsers in real
time, and cleans up after itself on a schedule.

**Stack:** Python 3.13 · FastAPI · SQLAlchemy 2 (async) · Alembic · PostgreSQL 17
(`tstzrange` + GiST exclusion constraint) · Redis 7 (locks, pub/sub, Celery broker) ·
Celery + Beat · WebSockets · Docker Compose.

The web client lives in its own repository, **hotel-mvp-frontend**, and consumes the REST and
WebSocket APIs documented here.

> **Reading this to review it?** Jump to [Review guide](#10-review-guide-for-humans-and-ai-reviewers):
> a reading order, a claim → evidence table and the known limitations.

## Contents

1. [Functional overview](#1-functional-overview)
2. [Lifecycles](#2-lifecycles)
3. [Architecture and code map](#3-architecture-and-code-map)
4. [Consistency and concurrency guarantees](#4-consistency-and-concurrency-guarantees)
5. [API reference](#5-api-reference)
6. [Configuration](#6-configuration)
7. [Getting started](#7-getting-started)
8. [Development workflow](#8-development-workflow)
9. [Operations](#9-operations)
10. [Review guide for humans and AI reviewers](#10-review-guide-for-humans-and-ai-reviewers)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Functional overview

### In one paragraph

A guest picks a room and a time range (a whole number of 30-minute blocks). The system
**holds** the range for 5 minutes (the "cart") so nobody else can take it while the guest
decides. The guest then **confirms** (or cancels, or lets the hold lapse). When the stay is
over, a background job marks the booking **completed** and flags the room **cleaning**; staff
mark it clean and it is **available** again. Every state change is broadcast over a WebSocket
so calendars update live.

### Glossary

| Term | Meaning |
|---|---|
| **Room** | A bookable unit: `number` (unique), `name`, `capacity`, `price_per_block_cents`, `status`. |
| **Block** | The atomic unit of time: 30 minutes, aligned to `:00` / `:30`. |
| **Time slot** | A half-open interval `[start, end)` made of 1..672 whole blocks (max two weeks). `10:00-10:30` and `10:30-11:00` do **not** overlap. |
| **Booking** | A guest's claim on a room for a time slot. Has a `status` and a price (`blocks x price_per_block_cents`). |
| **Hold / cart** | A `pending` booking that blocks its slot for `BOOKING_HOLD_TTL_SECONDS` (default 300) until confirmed. |
| **Housekeeping** | The scheduled sweeps run by Celery Beat every minute. |

### Business rules

| # | Rule |
|---|---|
| BR-1 | A booking covers whole 30-minute blocks on `:00` / `:30`, with timezone-aware instants. Anything else is rejected (`422 invalid_time_slot`). |
| BR-2 | Two *active* bookings (`pending`, `confirmed`, `checked_in`) of the same room can never overlap. Back-to-back is fine. Different rooms are independent. |
| BR-3 | Cancelled, expired and completed bookings release their slot immediately; history is kept. |
| BR-4 | A hold lasts 5 minutes. Confirming after that fails with `410 hold_expired` and marks the booking expired. |
| BR-5 | A room under `maintenance` cannot be booked (`409 room_not_bookable`). |
| BR-6 | Creating a booking is idempotent per `Idempotency-Key`: a retry returns the same booking; reusing the key with different data is rejected. |
| BR-7 | When a stay ends, its booking becomes `completed` and the room becomes `cleaning`, **unless** the room is in `maintenance`, already in `cleaning`, or another booking is in progress in it right now (back-to-back stays). |
| BR-8 | Staff can mark a room clean only from `cleaning` (`POST /rooms/{id}/cleaned`). Any other status is `409`. |
| BR-9 | The public calendar shows occupancy only, never guest names or e-mails. |

### Typical scenario

1. `POST /api/v1/bookings` for room 101, 10:00-11:00 -> `201`, status `pending`,
   `hold_expires_at = now + 5 min`; browsers receive `booking.hold_created`.
2. A second guest asks for 10:30-11:30 -> `409 slot_unavailable` (rejected by Redis in
   microseconds; if Redis were down, PostgreSQL would still reject it).
3. The first guest `POST .../confirm` -> `confirmed`; browsers receive `booking.confirmed`.
4. At 11:01 Beat runs: the booking becomes `completed`, room 101 becomes `cleaning`
   (events `booking.completed`, `room.status_changed`).
5. Staff `POST /rooms/{id}/cleaned` -> `available` (event `room.status_changed`).

---

## 2. Lifecycles

### Booking

```mermaid
stateDiagram-v2
    [*] --> pending: POST /bookings (hold, 5 min)
    pending --> confirmed: POST /confirm
    pending --> cancelled: POST /cancel
    pending --> expired: hold lapsed (inline or Beat)
    confirmed --> cancelled: POST /cancel
    confirmed --> completed: stay ended (Beat)
    confirmed --> checked_in: reserved in the model
    checked_in --> completed: stay ended (Beat)
    completed --> [*]
    cancelled --> [*]
    expired --> [*]
```

Only `pending`, `confirmed` and `checked_in` occupy time (they are in the exclusion
constraint). The transition table is `BOOKING_TRANSITIONS` in `app/domain/entities.py`.

### Room

```mermaid
stateDiagram-v2
    available --> cleaning: stay ended (Beat)
    available --> occupied: reserved in the model
    available --> maintenance
    occupied --> cleaning
    cleaning --> available: POST /rooms/{id}/cleaned
    cleaning --> maintenance
    maintenance --> available
```

`ROOM_TRANSITIONS` in `app/domain/entities.py`. `occupied` and `checked_in` exist in the
model but **no endpoint sets them yet** (see [limitations](#known-limitations-and-open-decisions)).

---

## 3. Architecture and code map

Hexagonal / Clean Architecture. Dependencies point **inwards**: `presentation` and
`infrastructure` know the application layer; the application layer knows the domain; the
domain knows nothing about frameworks.

```
presentation  (FastAPI routers, WebSocket)  ──┐
infrastructure (SQLAlchemy, Redis, Celery) ───┼──►  application (use cases + ports) ──► domain
core (settings, logging)                      ┘
```

```
app/
  main.py                      composition root: app factory, lifespan, health checks
  core/
    config.py                  pydantic-settings; every tunable comes from the environment
    logging.py                 one JSON log event per line on stdout
  domain/                      pure Python, no framework imports
    value_objects.py           TimeSlot: 30-min alignment, half-open overlap rules
    entities.py                Room, Booking, and their state-transition tables
    enums.py  events.py  exceptions.py
  application/                 use cases; talk to the outside only through ports.py
    ports.py                   Protocols: repositories, UnitOfWork, SlotHoldStore, EventPublisher
    booking_service.py         hold -> confirm / cancel (the concurrency core)
    housekeeping_service.py    finished bookings -> completed + room cleaning; expire stale holds
    room_service.py            staff action: cleaning -> available
    calendar_service.py        read model for the calendar board
    events.py                  builds booking events from entities
  infrastructure/
    db/models.py               ORM + EXCLUDE USING gist + CHECK constraints
    db/repositories.py         SQL adapters, row locks, DB error -> domain error translation
    db/session.py  base.py     engines (async for API, pool-less for Celery), naming convention
    cache/slot_holds.py        Redis cart: SET NX EX per block, MULTI/EXEC, Lua release
    events/                    wire.py (JSON contract), publisher.py, subscriber.py (pub/sub)
    tasks/                     celery_app.py (Beat schedule), housekeeping.py (the two tasks)
  presentation/
    api/                       bookings.py rooms.py calendar.py, schemas.py, errors.py, deps.py
    ws/                        router.py (/ws/events), manager.py (per-client queues)
alembic/versions/              0001 initial schema (hand-written, documents the guarantees)
scripts/seed_demo.sql          demo rooms
tests/unit  tests/integration  pure tests / tests against real PostgreSQL + Redis
```

### Services (Docker Compose)

| Service | Role |
|---|---|
| `postgres` | PostgreSQL 17, source of truth. Logs lock waits and deadlocks. |
| `redis` | Redis 7.4 with AOF and `noeviction` (locks and queued tasks must never be evicted). DBs: 0 locks + pub/sub, 1 Celery broker, 2 Celery results. |
| `migrate` | One-shot `alembic upgrade head`; the others wait for it. |
| `api` | FastAPI + WebSocket on `:8000`. |
| `celery-worker` | Executes housekeeping tasks. Scale freely. |
| `celery-beat` | The scheduler. **Exactly one instance** (more would enqueue every task N times). |

---

## 4. Consistency and concurrency guarantees

Defence in depth, outermost first. The first layer is an optimisation; the third is the
guarantee.

| Layer | Mechanism | Purpose |
|---|---|---|
| 1. Redis cart | `SET key owner NX EX 300` per block in one MULTI/EXEC; failed attempts roll back with a compare-and-delete Lua script | Fast, user-facing rejection; shields PostgreSQL from a thundering herd; implements the 5-minute hold |
| 2. Room row lock | `SELECT ... FOR SHARE` on the room while inserting | Bookers do not block each other, but the room cannot change under them |
| 3. **Exclusion constraint** | `EXCLUDE USING gist (room_id WITH =, during WITH &&) WHERE status IN (active)` + CHECKs for half-open, bounded, 30-min aligned ranges | **The authoritative guarantee.** Holds with Redis down, flushed or bypassed |
| 4. Row lock + version | `SELECT ... FOR UPDATE` on confirm/cancel; `version_id` column | API and Celery cannot lose each other's updates |

Other design decisions a reviewer should know:

* **Lock order is always `rooms` then `bookings`** (API and Celery) to rule out deadlocks.
* **Events are published only after the transaction commits**, never before.
* **Housekeeping is idempotent and concurrency-safe:** one transaction per room,
  `FOR UPDATE SKIP LOCKED`, every transition checks the current status.
* **Stale holds are expired inline** when a new request needs the slot, so a lapsed hold never
  blocks anyone for up to a minute waiting for Beat.
* **Redis hold keys are written only for rooms that exist**, so random ids cannot fill Redis
  (it runs with `noeviction`).
* **DB errors become domain errors by constraint name** (e.g. `ex_bookings_room_no_overlap`
  -> `SlotUnavailableError`); a test pins those names.

---

## 5. API reference

Interactive docs at `/docs` (disabled when `ENVIRONMENT=production`). Base path `/api/v1`.

### REST

| Method and path | Purpose | Success | Notes |
|---|---|---|---|
| `POST /bookings` | Hold a slot | `201` (`200` on idempotent replay) | Body: `room_id`, `start`, `end` (tz-aware), `guest_name`, `guest_email`. Header `Idempotency-Key` (8-64 chars, optional). |
| `GET /bookings/{id}` | Read a booking | `200` | |
| `POST /bookings/{id}/confirm` | `pending` -> `confirmed` | `200` | Idempotent. `410` if the hold lapsed. |
| `POST /bookings/{id}/cancel` | Free the slot | `200` | Idempotent. |
| `POST /rooms/{id}/cleaned` | `cleaning` -> `available` | `200` | Idempotent for `available`. Staff action; **no auth yet**. |
| `GET /calendar?from=&to=` | Rooms and their bookings in `[from, to)` | `200` | Max 14 days. Occupancy only, no guest data. |
| `GET /health/live`, `GET /health/ready` | Liveness / readiness (DB + Redis) | `200` / `503` | Outside `/api/v1`. |

Errors are `{"error": "<code>", "detail": "<message>"}`:

| HTTP | `error` | When |
|---|---|---|
| 404 | `not_found` | unknown room or booking |
| 409 | `slot_unavailable` | overlaps a hold or booking |
| 409 | `invalid_state_transition`, `room_not_bookable` | e.g. confirming a cancelled booking |
| 410 | `hold_expired` | confirming after the 5-minute hold |
| 422 | `invalid_time_slot`, `invalid_window`, `idempotency_key_reuse` | domain validation |
| 503 | `busy` | database lock timeout; `Retry-After: 1` |

Malformed request bodies (wrong types, missing fields, naive datetimes) are answered by
FastAPI's standard `422` with a `detail` list.

### WebSocket `GET /ws/events`

Server -> client only. Optional filter `?room_id=<uuid>` (repeatable). The `Origin` header
must be listed in `CORS_ORIGINS` (browsers do not apply CORS to WebSockets). Every API replica
subscribes to one Redis pub/sub channel (`EVENTS_CHANNEL`), so events published by any replica
or Celery worker reach every browser.

Envelope: `{"v": 1, "type": "...", "occurred_at": "<ISO-8601>", "room_id": "<uuid>|null", "data": {...}}`

| `type` | `data` | Emitted when |
|---|---|---|
| `booking.hold_created` | `booking_id, status, start, end, hold_expires_at` | a hold is created |
| `booking.confirmed` / `.cancelled` / `.completed` / `.expired` | same | the matching transition happens |
| `room.status_changed` | `status, previous_status` | housekeeping flags a room, or staff mark it clean |
| `system.connected` | `{}` | right after the handshake |
| `system.resync` | `{}` | after a Redis outage: events may be lost, refetch the snapshot |

Delivery is **best-effort**. The REST snapshot is the source of truth; events are incremental
hints. A client that cannot keep up is closed with code `1013`; it should reconnect and refetch.

---

## 6. Configuration

12-Factor: all configuration comes from the environment. `.env` is only a developer
convenience. `docker-compose.yml` builds the connection strings from the primitives below.

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `development` | `development` / `staging` / `production` (`production` hides `/docs`). |
| `LOG_LEVEL` | `INFO` | `DEBUG` .. `CRITICAL`. Logs are JSON on stdout. |
| `API_PORT` | `8000` | Published API port. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `hotel` / `change-me-in-production` / `hotel` | **Required** by compose. Change the password. |
| `POSTGRES_PORT`, `REDIS_PORT` | `5432`, `6379` | Published host ports. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `10`, `20` | API connection pool. |
| `BOOKING_HOLD_TTL_SECONDS` | `300` | Length of the hold (30..3600). |
| `EVENTS_CHANNEL` | `hotel:events` | Redis pub/sub channel. |
| `WS_MAX_QUEUE` | `256` | Per-WebSocket outbound buffer before a slow client is dropped. |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | Allowed origins (REST CORS **and** WebSocket `Origin`). JSON list or comma-separated. |

When running outside compose, the app also needs `DATABASE_URL` (`postgresql+psycopg://...`),
`REDIS_URL`, `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` (compose sets them). Optional
tuning with defaults: `DB_LOCK_TIMEOUT_MS` (5000), `DB_STATEMENT_TIMEOUT_MS` (15000),
`DB_POOL_TIMEOUT_SECONDS` (10).

---

## 7. Getting started

### Prerequisites

Docker with Compose v2 and `make` for the container path. For the local path: Python 3.13,
[`uv`](https://docs.astral.sh/uv/), PostgreSQL 16+ and Redis 7+.

### With Docker (recommended)

```bash
make up                       # creates .env from .env.example, builds and starts everything
curl localhost:8000/health/ready
make seed                     # demo rooms (the API has no endpoint to create rooms)
make logs                     # JSON log stream of every service
make down
```

### Try it

```bash
ROOM=$(curl -s "localhost:8000/api/v1/calendar?from=2030-01-01T00:00:00Z&to=2030-01-02T00:00:00Z" \
       | python3 -c "import sys,json; print(json.load(sys.stdin)['rooms'][0]['id'])")

# 1. hold 10:00-11:00 for five minutes
curl -s -XPOST localhost:8000/api/v1/bookings -H 'content-type: application/json' \
  -H 'Idempotency-Key: demo-key-0001' \
  -d '{"room_id":"'$ROOM'","start":"2030-01-01T10:00:00Z","end":"2030-01-01T11:00:00Z",
       "guest_name":"Ada","guest_email":"ada@example.com"}'
# -> 201 {"id":"...","status":"pending","blocks":2,"total_price_cents":3000,"hold_expires_at":"..."}

# 2. an overlapping request is refused
curl -s -w ' [%{http_code}]\n' -XPOST localhost:8000/api/v1/bookings -H 'content-type: application/json' \
  -d '{"room_id":"'$ROOM'","start":"2030-01-01T10:30:00Z","end":"2030-01-01T11:30:00Z",
       "guest_name":"Bob","guest_email":"bob@example.com"}'
# -> {"error":"slot_unavailable",...} [409]

# 3. watch events live (any WebSocket client), then confirm with POST /api/v1/bookings/<id>/confirm
```

### Without Docker

```bash
uv sync
export DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/hotel \
       REDIS_URL=redis://localhost:6379/0 \
       CELERY_BROKER_URL=redis://localhost:6379/1 \
       CELERY_RESULT_BACKEND=redis://localhost:6379/2
uv run alembic upgrade head
psql "postgresql://hotel:hotel@localhost:5432/hotel" -f scripts/seed_demo.sql
uv run uvicorn app.main:app --port 8000
# in two more terminals:
uv run celery --app=app.infrastructure.tasks.celery_app:celery_app worker --loglevel=INFO
uv run celery --app=app.infrastructure.tasks.celery_app:celery_app beat --loglevel=INFO
```

### Full stack with the frontend

1. Start this backend (`make up`, `make seed`). Keep `CORS_ORIGINS` set to the frontend's origin.
2. In **hotel-mvp-frontend**: `cp .env.example .env.local`, `npm install`, `npm run dev`.
3. Open <http://localhost:3000>. The frontend reads `API_BASE_URL` (server side) and `WS_URL`
   (the browser's WebSocket address); the defaults match this stack.

---

## 8. Development workflow

| Command | What it does |
|---|---|
| `make test` | All tests inside the `api` container (needs the stack up) |
| `make test-unit` | Unit tests only (no database) |
| `make lint` | `ruff check` + `ruff format --check` |
| `make typecheck` | `mypy --strict` over `app`, `tests`, `alembic` |
| `make migrate` / `make revision m="msg"` | Apply / autogenerate a migration |
| `make psql` | `psql` shell in the database container |

Local equivalent:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy app tests alembic
TEST_DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/postgres \
TEST_REDIS_URL=redis://localhost:6379/15 uv run pytest
```

Integration tests need `TEST_DATABASE_URL` (a role allowed to `CREATE DATABASE`; a throw-away
database is created and dropped) and `TEST_REDIS_URL` (use a spare logical DB: it is flushed).
Without them those tests are skipped.

**Conventions:** strict typing everywhere (`mypy --strict`); migrations are hand-reviewed
and never import the ORM models; constraint names are part of the contract; comments explain
concurrency decisions, not syntax.

### Tests (110 cases)

| Area | File | What it proves |
|---|---|---|
| Time slots, state machines | `tests/unit/` | block alignment, half-open overlap, allowed transitions, Beat schedule |
| Schema | `test_booking_constraints.py` | exclusion constraint, CHECKs, optimistic locking, models == migrations, rollback works |
| Redis cart | `test_slot_holds.py` | all-or-nothing acquisition, no partial keys, owner-safe release, one winner among 40 |
| Booking use cases | `test_booking_service.py` | 30-way races, DB as last defence, inline expiry, idempotency |
| Housekeeping | `test_housekeeping.py`, `test_celery_tasks.py` | completion rules, concurrent workers, `SKIP LOCKED`, real tasks with Redis events |
| Rooms and calendar | `test_room_service.py`, `test_calendar.py` | clean-only-from-cleaning, lifecycle, window rules, no guest data |
| HTTP and WebSocket | `test_bookings_api.py`, `test_websocket.py` | status codes, delivery, filtering, origin check, slow-client eviction |

---

## 9. Operations

* **Health:** `GET /health/live` (process up), `GET /health/ready` (DB and Redis reachable, else `503`).
* **Logs:** JSON, one event per line, on stdout (12-Factor XI). Fields passed as `extra=` become
  top-level keys.
* **Migrations:** `migrate` runs before the app; `alembic/env.py` takes a PostgreSQL advisory
  lock so concurrent migrators serialise. A test covers the downgrade -> upgrade round trip.
* **Beat:** exactly one replica. Every task is idempotent and queued runs expire after 55 s,
  so a worker outage causes no catch-up burst.
* **Shutdown:** the API closes WebSockets, the Redis pub/sub connection and the pools.
* **Capacity notes:** WebSocket fan-out is per process (each replica subscribes once); Redis
  `noeviction` makes writes fail loudly under memory pressure instead of dropping locks.

---

## 10. Review guide for humans and AI reviewers

### Suggested reading order (about 20 minutes)

1. This README, sections 1-4.
2. `app/domain/value_objects.py` and `entities.py`: the rules, no I/O.
3. `app/infrastructure/db/models.py` and `alembic/versions/*0001*`: the exclusion constraint.
4. `app/application/booking_service.py`: the concurrency core, annotated by layer.
5. `app/infrastructure/cache/slot_holds.py`: the Redis cart.
6. `app/application/housekeeping_service.py`: scheduled sweeps.
7. `app/presentation/ws/manager.py` and `app/infrastructure/events/`: real-time delivery.
8. The matching tests in section 8.

### Claim -> evidence

| Claim | Where it is enforced | Evidence (test) |
|---|---|---|
| No overlapping active bookings, ever | `models.py` EXCLUDE | `test_booking_constraints.py::test_concurrent_inserts_only_one_wins`; `test_booking_service.py::test_database_constraint_is_last_line_of_defence` |
| Redis cart is all-or-nothing and owner-safe | `slot_holds.py` | `test_slot_holds.py::test_overlap_is_rejected_and_leaves_no_partial_keys`, `::test_expired_holder_cannot_delete_the_new_owners_key` |
| A lapsed hold frees the slot at once | `booking_service.py` (`expire_stale_holds`) | `test_stale_pending_hold_is_expired_inline_so_slot_frees_immediately` |
| Retried POSTs never duplicate | `Idempotency-Key` + unique constraint | `test_concurrent_identical_requests_create_a_single_booking` |
| Housekeeping never double-completes | `SKIP LOCKED`, status checks | `test_concurrent_workers_complete_every_booking_exactly_once` |
| Back-to-back stays do not flap the room | `housekeeping_service.py` | `test_back_to_back_stay_keeps_the_room_in_use_until_the_last_one_ends` |
| "Mark clean" cannot pull a room out of maintenance | `room_service.py` | `test_rooms_that_are_not_being_cleaned_cannot_be_marked_clean` |
| A slow WebSocket cannot stall others | `ws/manager.py` | `test_slow_client_is_dropped_without_blocking_broadcast` |
| Calendar leaks no personal data | `schemas.py` | `test_http_contract_and_privacy` |

### Verification status (what has and has not been run)

* Ran and passing: `ruff`, `mypy --strict`, all 110 tests against **PostgreSQL 16** and Redis
  (compose uses PostgreSQL 17; nothing used is version specific).
* Smoke-tested end to end with a real API, a real Celery worker and Beat, and a browser.
* `docker compose config` validates, **but the images were not built or run** in the
  environment where this was written (no Docker daemon): treat `Dockerfile`,
  `docker-compose.yml` and `make seed` through Docker as unexercised until you run them.

### Known limitations and open decisions

* **No authentication or authorisation.** `POST /rooms/{id}/cleaned` is staff-only by
  nature and the WebSocket is open to allowed origins. Add an auth dependency before exposing.
* **No endpoint creates rooms**; use `make seed` or SQL.
* **`checked_in` and `occupied` are modelled but unreachable:** no check-in use case exists.
  Housekeeping therefore allows `available -> cleaning`.
* **Back-to-back stays get no cleaning in between** (BR-7); a product decision to revisit.
* **Events are best-effort** (Redis pub/sub is fire-and-forget); clients must resync.
* **Single Redis node assumed** (the cart's Lua script touches several keys; not Redis Cluster safe).
* **Payments are not modelled**: `confirm` is the whole "payment" step.
* No rate limiting; no pagination (the calendar is bounded to 14 days instead).

---

## 11. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Calendar is empty | No rooms exist: `make seed`. |
| `connection refused` / unhealthy `api` | `make logs`; check `migrate` finished and `POSTGRES_*` are set in `.env` (compose refuses to start without them). |
| WebSocket closes immediately (`1008`) | The page's origin is not in `CORS_ORIGINS`. |
| Everything returns `409 slot_unavailable` for a free slot | A hold from the last 5 minutes is still active; it frees itself, or cancel it. |
| Integration tests are skipped | `TEST_DATABASE_URL` / `TEST_REDIS_URL` are unset. |
| Beat tasks never run | `celery-beat` is not running, or the worker is down (`make logs`). |
