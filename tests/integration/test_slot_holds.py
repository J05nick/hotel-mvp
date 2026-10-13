"""Redis hold store: all-or-nothing acquisition, owner-safe release, TTL."""

from __future__ import annotations

import asyncio
import uuid
from uuid import UUID

import pytest
from redis.asyncio import Redis

from app.domain.value_objects import TimeSlot
from app.infrastructure.cache.slot_holds import RedisSlotHoldStore, block_keys
from tests.support import T0

pytestmark = pytest.mark.integration


@pytest.fixture
def store(redis: Redis) -> RedisSlotHoldStore:
    return RedisSlotHoldStore(redis)


async def test_acquire_sets_one_key_per_block_with_ttl(
    store: RedisSlotHoldStore, redis: Redis
) -> None:
    room, owner = uuid.uuid4(), uuid.uuid4()
    slot = TimeSlot.from_blocks(T0, 3)

    assert await store.acquire(room, slot, owner, 300)

    keys = block_keys(room, slot)
    assert len(keys) == 3
    for key in keys:
        assert await redis.get(key) == str(owner)
        assert 0 < await redis.ttl(key) <= 300


async def test_overlap_is_rejected_and_leaves_no_partial_keys(
    store: RedisSlotHoldStore, redis: Redis
) -> None:
    room, first, second = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    held = TimeSlot.from_blocks(T0 + 2 * (TimeSlot.from_blocks(T0, 1).duration), 1)  # block #3
    wanted = TimeSlot.from_blocks(T0, 4)  # blocks #1..#4, overlaps block #3

    assert await store.acquire(room, held, first, 300)
    assert not await store.acquire(room, wanted, second, 300)

    # Only the first owner's single key survives; blocks #1, #2, #4 were rolled back.
    remaining = [k async for k in redis.scan_iter(f"hold:{room}:*")]
    assert remaining == block_keys(room, held)
    assert await redis.get(remaining[0]) == str(first)


async def test_release_only_deletes_keys_owned_by_caller(
    store: RedisSlotHoldStore, redis: Redis
) -> None:
    room, owner, intruder = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    slot = TimeSlot.from_blocks(T0, 2)
    await store.acquire(room, slot, owner, 300)

    await store.release(room, slot, intruder)  # not the owner => no-op
    assert len(await redis.keys(f"hold:{room}:*")) == 2

    await store.release(room, slot, owner)
    assert await redis.keys(f"hold:{room}:*") == []


async def test_expired_holder_cannot_delete_the_new_owners_key(
    store: RedisSlotHoldStore, redis: Redis
) -> None:
    room, old_owner, new_owner = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    slot = TimeSlot.from_blocks(T0, 1)
    await store.acquire(room, slot, old_owner, 300)
    await redis.delete(*block_keys(room, slot))  # simulate TTL expiry
    assert await store.acquire(room, slot, new_owner, 300)

    await store.release(room, slot, old_owner)  # late cleanup by the stale holder

    assert await redis.get(block_keys(room, slot)[0]) == str(new_owner)


async def test_concurrent_acquire_has_exactly_one_winner(
    store: RedisSlotHoldStore, redis: Redis
) -> None:
    room = uuid.uuid4()
    owners: list[UUID] = [uuid.uuid4() for _ in range(40)]
    # Overlapping-but-different ranges stress the all-or-nothing batch.
    slots = [
        TimeSlot.from_blocks(T0 + i % 3 * TimeSlot.from_blocks(T0, 1).duration, 4)
        for i in range(40)
    ]

    results = await asyncio.gather(
        *(store.acquire(room, s, o, 300) for s, o in zip(slots, owners, strict=True))
    )

    assert results.count(True) == 1
    winner = owners[results.index(True)]
    values = {await redis.get(k) async for k in redis.scan_iter(f"hold:{room}:*")}
    assert values == {str(winner)}
