"""Redis implementation of the reservation "cart" (distributed lock per block).

Model
-----
Every 30-minute block of a room is one Redis key:

    hold:{room_id}:{block_start_unix_seconds}  ->  {owner_booking_id}   (TTL)

A hold on a multi-block range is the set of its block keys, all owned by the
same booking id.

Why it is safe
--------------
* Acquisition is `SET key owner NX EX ttl` for every block, sent inside one
  MULTI/EXEC transaction. Redis executes the whole batch without interleaving
  other clients' commands, so competing requests are strictly serialised: the
  first request that finds every block free wins; later ones see its keys.
* If any block was already taken, the blocks this request did manage to set are
  released again (compare-and-delete) so a failed attempt leaves nothing behind.
  A third request may observe those blocks as busy for a few microseconds; that
  is a *false conflict*, never a double booking.
* Release is a Lua compare-and-delete: a holder whose TTL lapsed can never
  delete the key that now belongs to somebody else.
* The TTL is the safety net: a crashed API process cannot leave a room blocked
  for more than `booking_hold_ttl_seconds`.

Redis is the fast, user-facing gate. It is NOT the source of truth: the
PostgreSQL EXCLUDE constraint still rejects any overlap if Redis is down,
flushed or bypassed. Keys require a single Redis node (no cross-slot scripts).
"""

from __future__ import annotations

import logging
from typing import Final, cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.domain.value_objects import TimeSlot

logger = logging.getLogger(__name__)

# KEYS = block keys, ARGV[1] = owner. Deletes only keys still owned by `owner`.
_RELEASE_LUA: Final[str] = """
local released = 0
for _, key in ipairs(KEYS) do
    if redis.call('GET', key) == ARGV[1] then
        redis.call('DEL', key)
        released = released + 1
    end
end
return released
"""


def block_keys(room_id: UUID, slot: TimeSlot) -> list[str]:
    """Deterministic, sorted keys for each 30-minute block of `slot`."""
    return [f"hold:{room_id}:{int(block.start.timestamp())}" for block in slot.iter_blocks()]


class RedisSlotHoldStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._release_script = redis.register_script(_RELEASE_LUA)

    async def acquire(self, room_id: UUID, slot: TimeSlot, owner: UUID, ttl_seconds: int) -> bool:
        keys = block_keys(room_id, slot)
        owner_value = str(owner)

        # CONCURRENCY: MULTI/EXEC => the SET NX EX batch is atomic w.r.t. other clients.
        async with self._redis.pipeline(transaction=True) as pipe:
            for key in keys:
                pipe.set(key, owner_value, nx=True, ex=ttl_seconds)
            results = cast("list[bool | None]", await pipe.execute())

        if all(results):
            return True

        # Lost the race (or a block was already held): undo our partial acquisition.
        await self.release(room_id, slot, owner)
        return False

    async def release(self, room_id: UUID, slot: TimeSlot, owner: UUID) -> None:
        keys = block_keys(room_id, slot)
        try:
            await self._release_script(keys=keys, args=[str(owner)])
        except RedisError:
            # Never fail a business operation because cleanup failed: the TTL expires the keys.
            logger.exception("holds.release_failed", extra={"room_id": str(room_id)})
