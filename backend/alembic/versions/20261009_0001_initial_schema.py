"""Initial schema: rooms, bookings, tstzrange + GiST exclusion constraint.

Revision ID: 0001
Revises:
Create Date: 2026-10-09 00:00:00+00:00

Hand-written on purpose: autogenerate does not emit extensions, and an
explicit migration documents the concurrency guarantees this schema provides.
Migrations are frozen snapshots — they deliberately do NOT import the ORM
models, so later model edits can never rewrite history.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# NOTE: `op.f()` marks a name as final. Without it, the metadata naming
# convention (`ck_%(table_name)s_%(constraint_name)s`) is applied a second time.

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROOM_STATUSES = ("available", "occupied", "cleaning", "maintenance")
BOOKING_STATUSES = ("pending", "confirmed", "checked_in", "completed", "cancelled", "expired")
ACTIVE_BOOKING_PREDICATE = "status IN ('checked_in', 'confirmed', 'pending')"
BLOCK_SECONDS = 1800  # 30 minutes


def upgrade() -> None:
    # btree_gist lets a GiST index combine scalar equality (room_id =) with
    # range overlap (during &&) in one EXCLUDE constraint.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    room_status = postgresql.ENUM(*ROOM_STATUSES, name="room_status", create_type=False)
    booking_status = postgresql.ENUM(*BOOKING_STATUSES, name="booking_status", create_type=False)
    room_status.create(op.get_bind(), checkfirst=True)
    booking_status.create(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------ rooms
    op.create_table(
        "rooms",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("number", sa.String(16), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("capacity", sa.SmallInteger(), nullable=False),
        sa.Column("price_per_block_cents", sa.Integer(), nullable=False),
        sa.Column("status", room_status, server_default="available", nullable=False),
        sa.Column("version_id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_rooms"),
        sa.UniqueConstraint("number", name="uq_rooms_number"),
        sa.CheckConstraint("capacity > 0", name=op.f("ck_rooms_capacity_positive")),
        sa.CheckConstraint("price_per_block_cents >= 0", name=op.f("ck_rooms_price_non_negative")),
    )
    op.create_index("ix_rooms_status", "rooms", ["status"])

    # --------------------------------------------------------------- bookings
    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("guest_name", sa.String(120), nullable=False),
        sa.Column("guest_email", sa.String(254), nullable=False),
        sa.Column("during", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("status", booking_status, server_default="pending", nullable=False),
        sa.Column("total_price_cents", sa.Integer(), nullable=False),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=True),
        sa.Column("version_id", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_bookings"),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name="fk_bookings_room_id_rooms", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_bookings_idempotency_key"),
        # ==================================================================
        # CONCURRENCY-CRITICAL — the anti double-booking guarantee.
        # Two ACTIVE bookings for the same room can never have overlapping
        # ranges. Enforced atomically by PostgreSQL (SQLSTATE 23P01), so it
        # is immune to races between API replicas, workers or manual SQL.
        # ==================================================================
        postgresql.ExcludeConstraint(
            (sa.column("room_id"), "="),
            (sa.column("during"), "&&"),
            name="ex_bookings_room_no_overlap",
            using="gist",
            where=sa.text(ACTIVE_BOOKING_PREDICATE),
        ),
        sa.CheckConstraint(
            "NOT isempty(during) AND NOT lower_inf(during) AND NOT upper_inf(during)",
            name=op.f("ck_bookings_during_bounded"),
        ),
        sa.CheckConstraint(
            "lower_inc(during) AND NOT upper_inc(during)",
            name=op.f("ck_bookings_during_half_open"),
        ),
        sa.CheckConstraint(
            f"mod(extract(epoch FROM lower(during)), {BLOCK_SECONDS}) = 0 "
            f"AND mod(extract(epoch FROM upper(during)), {BLOCK_SECONDS}) = 0",
            name=op.f("ck_bookings_during_block_aligned"),
        ),
        sa.CheckConstraint(
            "total_price_cents >= 0", name=op.f("ck_bookings_total_price_non_negative")
        ),
        sa.CheckConstraint(
            "(status = 'pending') OR (hold_expires_at IS NULL)",
            name=op.f("ck_bookings_hold_only_when_pending"),
        ),
    )
    op.create_index("ix_bookings_room_id", "bookings", ["room_id"])
    op.create_index("ix_bookings_during", "bookings", ["during"], postgresql_using="gist")
    op.create_index(
        "ix_bookings_status_end", "bookings", [sa.text("status"), sa.text("upper(during)")]
    )
    op.create_index(
        "ix_bookings_pending_hold_expires_at",
        "bookings",
        ["hold_expires_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Keep `updated_at` correct even for writes that bypass the ORM
    # (Celery bulk UPDATEs, manual SQL).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END;
        $$;
        """
    )
    for table in ("rooms", "bookings"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in ("bookings", "rooms"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.drop_table("bookings")
    op.drop_table("rooms")
    postgresql.ENUM(name="booking_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="room_status").drop(op.get_bind(), checkfirst=True)
    # btree_gist is intentionally left installed: other objects may depend on it.
