# Developer entrypoints. Full playbook arrives in FASE 5 (README.md).
COMPOSE ?= docker compose
BACKEND  = $(COMPOSE) exec api

.PHONY: env up down logs migrate revision test test-unit lint typecheck psql

env:            ## Create .env from the template (no overwrite)
	@test -f .env || cp .env.example .env

up: env         ## Build and start the full stack
	$(COMPOSE) up --build -d

down:           ## Stop the stack (keeps volumes)
	$(COMPOSE) down

logs:           ## Tail the JSON log stream of every service
	$(COMPOSE) logs -f

migrate:        ## Apply pending Alembic migrations
	$(COMPOSE) run --rm migrate

revision:       ## Create a migration: make revision m="add something"
	$(BACKEND) alembic revision --autogenerate -m "$(m)"

test:           ## Unit + integration tests inside the api container
	$(BACKEND) sh -c 'TEST_DATABASE_URL=$${DATABASE_URL%/*}/postgres pytest -q'

test-unit:      ## Unit tests only (no database required)
	$(BACKEND) pytest -q tests/unit

lint:           ## Ruff lint + format check
	$(BACKEND) sh -c 'ruff check . && ruff format --check .'

typecheck:      ## mypy --strict
	$(BACKEND) mypy app tests alembic

psql:           ## Open a psql shell
	$(COMPOSE) exec postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'
