# hotel-mvp-backend

Backend of the hotel booking system sold in **30-minute blocks**. FastAPI +
SQLAlchemy + PostgreSQL (`tstzrange` / GiST exclusion constraints) + Redis +
Celery. The Next.js client lives in its own repository, **hotel-mvp-frontend**.

> Status: **FASE 1** (infrastructure and data model). Booking service,
> WebSockets and Celery Beat tasks arrive in FASE 2 and 3; the full playbook
> README arrives in FASE 5.

## Quick start

```bash
make up          # creates .env from .env.example, builds and starts the stack
curl localhost:8000/health/ready
make test        # unit + integration tests inside the api container
```

Without Docker (needs local PostgreSQL 16+ and Redis):

```bash
uv sync
export DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/hotel \
       REDIS_URL=redis://localhost:6379/0 \
       CELERY_BROKER_URL=redis://localhost:6379/1 \
       CELERY_RESULT_BACKEND=redis://localhost:6379/2
uv run alembic upgrade head
TEST_DATABASE_URL=postgresql+psycopg://hotel:hotel@localhost:5432/postgres uv run pytest
```

## Layout (Hexagonal / Clean Architecture)

```
app/
  core/            settings (12-Factor) and JSON logging to stdout
  domain/          entities, value objects, state machines — no framework imports
  infrastructure/  adapters: SQLAlchemy (db/), Redis (cache/), Celery (tasks/)
  main.py          FastAPI composition root
alembic/           migrations (0001: rooms, bookings, GiST exclusion constraint)
tests/             unit/ (pure) and integration/ (real PostgreSQL)
```

## Anti double-booking guarantee

`bookings.during` is a half-open `tstzrange`; PostgreSQL enforces
`EXCLUDE USING gist (room_id WITH =, during WITH &&)` over active statuses, so
overlapping reservations are impossible regardless of application races.
