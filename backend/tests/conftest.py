from __future__ import annotations

import os

# Unit tests must not depend on a real `.env`; provide harmless defaults.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://hotel:hotel@localhost:5432/hotel")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/1")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
