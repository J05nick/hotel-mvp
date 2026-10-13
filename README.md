# hotel-mvp-backend

Backend of the hotel booking system sold in **30-minute blocks**. FastAPI +
SQLAlchemy + PostgreSQL (`tstzrange` / GiST exclusion constraints) + Redis +
Celery. The Next.js client lives in its own repository, **hotel-mvp-frontend**,
and consumes the REST and WebSocket APIs documented below.

## Quick start

```bash
make up          # creates .env from .env.example, builds and starts the stack
curl localhost:8000/health/ready
make test        # unit + integration tests inside the api container
                 # (integration tests also need TEST_REDIS_URL, e.g. redis://redis:6379/15)
```

Without Docker (needs local PostgreSQL 16+ and Redis):

```bash
uv sync
export DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/hotel \
       REDIS_URL=redis://localhost:6379/0 \
       CELERY_BROKER_URL=redis://localhost:6379/1 \
       CELERY_RESULT_BACKEND=redis://localhost:6379/2
uv run alembic upgrade head
TEST_DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/postgres \
TEST_REDIS_URL=redis://localhost:6379/15 uv run pytest
```

## Layout (Hexagonal / Clean Architecture)

```
app/
  core/            settings (12-Factor) and JSON logging to stdout
  domain/          entities, value objects, state machines, events — no framework imports
  application/     use cases (BookingService) and the ports they depend on
  infrastructure/  adapters: SQLAlchemy (db/), Redis holds (cache/),
                   Redis pub/sub (events/), Celery (tasks/)
  presentation/    REST routers (api/) and the WebSocket endpoint (ws/)
  main.py          FastAPI composition root
alembic/           migrations (0001: rooms, bookings, GiST exclusion constraint)
tests/             unit/ (pure) and integration/ (real PostgreSQL)
```

## Anti double-booking guarantee

`bookings.during` is a half-open `tstzrange`; PostgreSQL enforces
`EXCLUDE USING gist (room_id WITH =, during WITH &&)` over active statuses, so
overlapping reservations are impossible regardless of application races.

## Reservation flow

```
POST /api/v1/bookings            hold a range for 5 min  -> 201 (200 on Idempotency-Key replay)
POST /api/v1/bookings/{id}/confirm   PENDING -> CONFIRMED   (410 if the hold expired)
POST /api/v1/bookings/{id}/cancel    frees the range        (idempotent)
GET  /api/v1/bookings/{id}
```

`start` / `end` are tz-aware, on `:00` / `:30`, half-open `[start, end)`. Send an
`Idempotency-Key` header (8-64 chars) so network retries never create two bookings.

| Status | `error` | Meaning |
|---|---|---|
| 404 | `not_found` | unknown room / booking |
| 409 | `slot_unavailable` | range overlaps a hold or booking |
| 409 | `invalid_state_transition`, `room_not_bookable` | e.g. confirming a cancelled booking |
| 410 | `hold_expired` | the 5-minute cart lapsed before confirming |
| 422 | `invalid_time_slot`, `idempotency_key_reuse` | malformed range / key reused with other data |
| 503 | `busy` | DB lock timeout, retry (`Retry-After: 1`) |

### Concurrency layers (outermost first)

1. **Redis cart** — `SET key owner NX EX 300` per 30-min block, batched in MULTI/EXEC;
   all-or-nothing with compare-and-delete rollback (`infrastructure/cache/slot_holds.py`).
2. **`SELECT ... FOR SHARE`** on the room row while inserting.
3. **`EXCLUDE USING gist`** — the authoritative guarantee; still holds if Redis is down.
4. **`SELECT ... FOR UPDATE` + `version_id`** for confirm / cancel / Celery transitions.

Lock order is always `rooms` then `bookings`.

## Calendar snapshot

```
GET /api/v1/calendar?from=<ISO tz-aware>&to=<ISO tz-aware>   -> rooms + bookings of the window
```

The board's initial state (the live changes then arrive over the WebSocket). The window is
half-open `[from, to)` and at most 14 days (`422 invalid_window` otherwise). Each room carries
its `status` and the bookings overlapping the window in `pending`, `confirmed`, `checked_in`
or `completed`; cancelled / expired bookings and lapsed holds are omitted. `generated_at`
is the server clock at read time. The response contains occupancy only, **never guest data**:
the endpoint is unauthenticated.

## Real-time events (WebSocket)

`ws://localhost:8000/ws/events` (optionally `?room_id=<uuid>&room_id=<uuid>`).
The `Origin` header must be listed in `CORS_ORIGINS`. Every API replica
subscribes to one Redis pub/sub channel (`EVENTS_CHANNEL`), so events published
by any replica or Celery worker reach all browsers.

```json
{"v": 1, "type": "booking.confirmed", "occurred_at": "2031-03-01T10:00:00Z",
 "room_id": "…", "data": {"booking_id": "…", "status": "confirmed",
                          "start": "…", "end": "…", "hold_expires_at": null}}
```

| `type` | `data` |
|---|---|
| `booking.hold_created` / `.confirmed` / `.cancelled` / `.completed` / `.expired` | `booking_id, status, start, end, hold_expires_at` |
| `room.status_changed` | `status, previous_status` |
| `system.connected` | `{}` — sent right after the handshake |
| `system.resync` | `{}` — events may have been lost: refetch the REST snapshot |

Delivery is best-effort: treat the REST snapshot as the source of truth. A client
that cannot keep up is closed with code `1013`; reconnect and refetch.

## Scheduled housekeeping (Celery Beat)

Beat enqueues two tasks every 60 s (`app/infrastructure/tasks/celery_app.py`);
run **exactly one** `celery-beat` container, scale `celery-worker` freely.

| Task | What it does |
|---|---|
| `housekeeping.complete_finished_bookings` | `CONFIRMED` / `CHECKED_IN` bookings whose range ended -> `COMPLETED`; their room -> `CLEANING`; emits `booking.completed` and `room.status_changed` over the WebSocket |
| `housekeeping.expire_stale_holds` | `PENDING` bookings whose 5-minute hold lapsed -> `EXPIRED`; emits `booking.expired` |

Rules worth knowing:

* **`AVAILABLE -> CLEANING` is allowed.** Time-block stays have no explicit check-in, so a
  finished booking dirties an `AVAILABLE` room directly. Rooms in `MAINTENANCE` or
  already `CLEANING` keep their status (the booking still completes).
* **Back-to-back stays:** while the next guest's booking is in progress the room is not
  flagged; it turns `CLEANING` when the last consecutive stay ends.
* **Safe to overlap and repeat:** one transaction per room, `FOR UPDATE SKIP LOCKED`,
  lock order rooms -> bookings, and every transition checks the current status. Two
  workers or a slow tick never double-complete or double-emit.
* **Stale ticks are dropped:** queued runs expire after 55 s so a worker outage does not
  cause a burst of catch-up runs (the next tick does the same work).
* Tasks reuse the API's async use cases through `asyncio.run` with a private, pool-less
  engine and Redis client per run.

## Room cleaning

```
POST /api/v1/rooms/{id}/cleaned     CLEANING -> AVAILABLE   (200 RoomResponse)
```

Closes the loop of a stay: booking ends -> Beat flags the room `CLEANING` -> staff call
this endpoint -> the room is `AVAILABLE` again. It emits `room.status_changed`
(`previous_status: "cleaning"`) so every connected calendar updates.

* Idempotent: an already `AVAILABLE` room returns 200 and emits nothing.
* Only valid from `CLEANING`: `OCCUPIED` and `MAINTENANCE` rooms answer
  `409 invalid_state_transition` (ending maintenance is a different action).
* Takes an exclusive row lock on the room, so it serialises with the Celery sweep.
* Staff-only by nature: the MVP has no authentication yet; add an auth dependency in
  `app/presentation/api/rooms.py` before exposing it publicly.
