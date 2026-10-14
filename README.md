# hotel-mvp-backend · `main-lite`

**Demo edition of the hotel booking backend: FastAPI + PostgreSQL, nothing else.**
Rooms are sold in **30-minute blocks**; double bookings are impossible; every change reaches
connected browsers live.

This branch keeps all of the business logic of the full version and drops the infrastructure
around it: **no Redis, no Celery, no Alembic**. It exposes the **same REST and WebSocket
contract**, so the existing **hotel-mvp-frontend** works against it unchanged.

> The full edition (Redis cart, Celery Beat, pub/sub fan-out, migrations, hexagonal layers) lives
> on branch `ccr-7edda553-k4m5ko`. Use that one for anything beyond a demo.

## Run it

**Docker** (PostgreSQL + API, four demo rooms are created on first start):

```bash
docker compose up --build        # API on http://localhost:8000  ·  docs on /docs
```

**Without Docker** (needs Python 3.13, [`uv`](https://docs.astral.sh/uv/) and a PostgreSQL 16+):

```bash
createdb hotel                    # default DATABASE_URL: user hotel, password hotel, database hotel
uv sync
cp .env.example .env              # DATABASE_URL, demo seeding on
uv run uvicorn app.main:app --reload
```

Tables and the demo rooms are created at startup; there is nothing else to set up.

### Demo script

```bash
ROOM=$(curl -s "localhost:8000/api/v1/calendar?from=2030-01-01T00:00:00Z&to=2030-01-02T00:00:00Z" \
       | python3 -c "import sys,json; print(json.load(sys.stdin)['rooms'][0]['id'])")

# 1. Hold 10:00-11:00 for five minutes (the reservation "cart")
curl -s -XPOST localhost:8000/api/v1/bookings -H 'content-type: application/json' \
  -d '{"room_id":"'$ROOM'","start":"2030-01-01T10:00:00Z","end":"2030-01-01T11:00:00Z",
       "guest_name":"Ada","guest_email":"ada@example.com"}'
# -> 201 {"id":"...","status":"pending","blocks":2,"total_price_cents":3000,"hold_expires_at":"..."}

# 2. Somebody else wants 10:30-11:30: refused
curl -s -w ' [%{http_code}]\n' -XPOST localhost:8000/api/v1/bookings -H 'content-type: application/json' \
  -d '{"room_id":"'$ROOM'","start":"2030-01-01T10:30:00Z","end":"2030-01-01T11:30:00Z",
       "guest_name":"Bob","guest_email":"bob@example.com"}'
# -> {"error":"slot_unavailable",...} [409]

# 3. Confirm, cancel, mark a cleaning room clean
curl -s -XPOST localhost:8000/api/v1/bookings/<id>/confirm
curl -s -XPOST localhost:8000/api/v1/bookings/<id>/cancel
curl -s -XPOST localhost:8000/api/v1/rooms/<room-id>/cleaned
```

**With the frontend:** in `hotel-mvp-frontend` run `cp .env.example .env.local && npm install &&
npm run dev`, open <http://localhost:3000>, and repeat the curl calls above: the board updates
live. To see housekeeping, lower `HOUSEKEEPING_INTERVAL_SECONDS` (e.g. `10`).

## What it does

| Rule | |
|---|---|
| Blocks | A booking is a whole number of 30-minute blocks on `:00` / `:30`, timezone-aware, up to two weeks. `10:00-10:30` and `10:30-11:00` do not overlap. |
| No double booking | Two *active* bookings (`pending`, `confirmed`, `checked_in`) of one room can never overlap. Different rooms are independent. |
| Hold (the cart) | `POST /bookings` creates a `pending` booking that blocks the range for 5 minutes. Confirming later gives `410 hold_expired`. |
| Released ranges | `cancelled`, `expired` and `completed` bookings free their range; history is kept. |
| Maintenance | A room under `maintenance` cannot be booked. |
| Idempotency | The `Idempotency-Key` header makes retries return the same booking (`200`); reusing the key with other data is `422`. |
| Housekeeping | Every minute: a stay that ended becomes `completed` and its room `cleaning` (unless the room is in maintenance, already cleaning, or another stay is in progress); a lapsed hold becomes `expired`. |
| Cleaning | Staff call `POST /rooms/{id}/cleaned`: only `cleaning -> available`. |
| Privacy | The calendar shows occupancy only, never guest names or e-mails. |

Lifecycle: `pending -> confirmed -> completed`, with `cancelled` (from `pending` or `confirmed`)
and `expired` (from `pending`) as exits. Rooms: `available -> cleaning -> available`, plus
`maintenance`.

## API

Base path `/api/v1`. Interactive docs at `/docs`. Health: `/health/live`, `/health/ready`.

| Method and path | Purpose | Success |
|---|---|---|
| `POST /bookings` | Hold a range. Body: `room_id, start, end, guest_name, guest_email`. Header `Idempotency-Key` (8-64 chars, optional) | `201` (`200` on replay) |
| `GET /bookings/{id}` | Read a booking | `200` |
| `POST /bookings/{id}/confirm` | `pending -> confirmed` (idempotent) | `200` |
| `POST /bookings/{id}/cancel` | Free the range (idempotent) | `200` |
| `POST /rooms/{id}/cleaned` | `cleaning -> available` (idempotent for `available`) | `200` |
| `GET /calendar?from=&to=` | Rooms with their bookings in `[from, to)`, max 14 days | `200` |

Errors are `{"error": "<code>", "detail": "<message>"}`:
`404 not_found` · `409 slot_unavailable | invalid_state_transition | room_not_bookable` ·
`410 hold_expired` · `422 invalid_time_slot | invalid_window | idempotency_key_reuse` ·
`503 busy` (database lock timeout, retry). Malformed bodies get FastAPI's standard `422`.

**WebSocket** `GET /ws/events` (optional `?room_id=<uuid>`, repeatable; the `Origin` header must
be in `CORS_ORIGINS`). Server -> client only. Envelope
`{"v":1,"type":"...","occurred_at":"...","room_id":"...","data":{...}}`:

| `type` | `data` |
|---|---|
| `booking.hold_created` `.confirmed` `.cancelled` `.completed` `.expired` | `booking_id, status, start, end, hold_expires_at` |
| `room.status_changed` | `status, previous_status` |
| `system.connected` | `{}` (right after the handshake) |

## How it works

| File | Role |
|---|---|
| `app/models.py` | The two tables, **including the exclusion constraint** that makes double booking impossible, and CHECKs for the 30-minute grid |
| `app/services.py` | All business logic: holds, confirm, cancel, cleaning, calendar, the two housekeeping sweeps |
| `app/routers.py` | Endpoints and the WebSocket |
| `app/events.py` | In-memory hub that fans events out to WebSockets |
| `app/main.py` | App wiring, startup (schema, demo data) and the background sweeper |
| `app/schemas.py`, `errors.py`, `db.py`, `config.py` | Models, one error type, engine, settings |

**Preventing double bookings with PostgreSQL alone**

* `EXCLUDE USING gist (room_id WITH =, during WITH &&) WHERE status IN (active)`: PostgreSQL
  rejects an overlapping `INSERT` atomically, however many requests race. The API turns that
  error into `409 slot_unavailable`.
* A hold is simply a `pending` row with a deadline, so it blocks the range like any booking.
  A lapsed hold is expired on the spot by the next request that needs the range.
* `confirm` / `cancel` lock the booking row (`SELECT ... FOR UPDATE`) so they cannot interleave.
* Housekeeping uses `FOR UPDATE SKIP LOCKED` and checks current status, so running it twice, or
  alongside a request, never double-processes anything.
* Events are published only after the transaction commits.

## Configuration

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://hotel:hotel@localhost:5432/hotel` | |
| `BOOKING_HOLD_TTL_SECONDS` | `300` | Length of a hold |
| `HOUSEKEEPING_INTERVAL_SECONDS` | `60` | Sweeper period; `0` turns it off |
| `SEED_DEMO_DATA` | `false` (`.env.example`: `true`) | Insert four demo rooms when the table is empty |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | REST CORS **and** WebSocket `Origin`; JSON list or comma-separated |

## Tests

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy app tests
TEST_DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/postgres uv run pytest
```

53 tests against a real PostgreSQL (a throw-away database is created and dropped; the role
needs `CREATEDB`). Without `TEST_DATABASE_URL` they are skipped. They cover, among others: 20
simultaneous requests for one slot -> exactly one `201`; identical retries racing -> one
booking; the constraint and CHECKs straight at the database; lapsed holds; the sweeps
(including concurrent ones); room cleaning rules; calendar privacy; WebSocket delivery,
room filter and origin check.

## Differences from the full edition (and demo limitations)

| Full edition | This branch |
|---|---|
| Redis "cart" in front of the database | Holds live in PostgreSQL; every attempt reaches the database |
| Celery worker + Beat | One `asyncio` task inside the API, every `HOUSEKEEPING_INTERVAL_SECONDS` |
| Redis pub/sub, any number of replicas | In-memory hub: **run a single API process**; with several workers a browser only sees events from its own worker |
| Alembic migrations | `create_all` at startup; schema changes need a manual migration or a fresh database |
| Hexagonal layers, unit-of-work, optimistic `version_id` | Flat modules; row locks only |
| JSON logs, health with Redis, resync after outages | Plain logging; no resync message (the frontend still reloads after a reconnect) |

Also: no authentication (`/rooms/{id}/cleaned` is a staff action), no rate limiting, no endpoint
to create rooms (demo rooms are seeded; use SQL otherwise), and `checked_in` / `occupied` exist in
the schema but nothing sets them.

## Verification status

* `ruff`, `mypy --strict` and the 53 tests pass on PostgreSQL 16 (compose uses 17).
* This branch was also run **without Redis, on a brand-new database**, with the real frontend in
  Chromium: live hold / confirm / cancel / cleaning, a refused overlapping booking, the API
  killed and restarted with a change made while offline, day navigation, dark mode and mobile:
  25/25 checks, no console errors. The in-process sweeper was observed completing a stay,
  expiring a hold and flagging the room, with the matching WebSocket events.
* `docker compose config` validates, **but the image was not built or run** (no Docker daemon in
  the authoring environment).
