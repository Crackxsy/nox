"""Pairing lifecycle (ST-17-01, Spec v0.8 §3.1/§9): issue, redeem, expire, re-use, race, revoke.

The negative paths matter more than the happy one here, so each of them is its own test and each
asserts that no device row was created.
"""

from __future__ import annotations

import asyncio

from nox.remote.pairing import CODE_ALPHABET, CODE_LENGTH, PairingService, generate_code

SENDER = "987654321"  # distinctive: a substring check below must not hit hex by luck


async def test_code_shape_is_unambiguous_and_single_use_sized():
    codes = {generate_code() for _ in range(200)}

    assert all(len(c) == CODE_LENGTH for c in codes)
    assert all(set(c) <= set(CODE_ALPHABET) for c in codes)
    assert not set(CODE_ALPHABET) & set("01ILOU")  # no character pair a human can confuse
    assert len(codes) > 190  # generated, not counted out


async def test_pair_start_never_persists_or_publishes_the_code(pairing, repo, db, bus):
    start = await pairing.start(device_name="Pixel")

    rows = db.fetch_all("SELECT * FROM remote_pairings")
    assert len(rows) == 1
    assert start.code not in str(dict(rows[0]))
    published = [e for e in bus.published if e.name == "remote.pairing_started"]
    assert published and start.code not in str(published[0].payload)


async def test_redeem_creates_exactly_one_device(pairing, repo, bus):
    start = await pairing.start(device_name="Pixel")

    result = await pairing.redeem(start.code, sender_id=SENDER)

    assert result.ok and result.name == "Pixel"
    devices = repo.list_devices()
    assert len(devices) == 1
    assert devices[0].id == result.device_id
    assert [e.name for e in bus.published if e.name == "remote.paired"]


async def test_the_stored_device_key_is_a_hash_not_the_sender_id(pairing, repo, db):
    start = await pairing.start()
    await pairing.redeem(start.code, sender_id=SENDER)

    row = db.fetch_all("SELECT * FROM paired_devices")[0]
    dumped = str(dict(row))

    assert SENDER not in dumped  # the Telegram account id is nowhere in the row
    assert len(str(row["public_key"])) == 64  # sha256 hex
    assert repo.find_device_for_sender("telegram", SENDER) is not None
    assert repo.find_device_for_sender("telegram", "999") is None


async def test_expired_code_is_rejected_and_creates_no_device(pairing, repo, clock, audit):
    start = await pairing.start()
    clock.advance(301)

    result = await pairing.redeem(start.code, sender_id=SENDER)

    assert not result.ok and result.reason == "expired"
    assert repo.list_devices() == []
    assert audit.entries[-1]["decision"] == "deny"


async def test_used_code_cannot_be_redeemed_twice(pairing, repo):
    start = await pairing.start()
    await pairing.redeem(start.code, sender_id=SENDER)

    second = await pairing.redeem(start.code, sender_id="777")

    assert not second.ok and second.reason == "used"
    assert len(repo.list_devices(include_revoked=False)) == 1


async def test_unknown_code_is_rejected_and_audited(pairing, repo, audit):
    result = await pairing.redeem("ZZZZZZZZ", sender_id=SENDER)

    assert not result.ok and result.reason == "unknown_code"
    assert repo.list_devices() == []
    assert audit.entries[-1]["result"] == "unknown_code"


async def test_two_racing_redemptions_produce_exactly_one_device(pairing, repo):
    start = await pairing.start()

    first, second = await asyncio.gather(
        pairing.redeem(start.code, sender_id="111"),
        pairing.redeem(start.code, sender_id="222"),
    )

    assert sorted([first.ok, second.ok]) == [False, True]
    assert len(repo.list_devices(include_revoked=False)) == 1


async def test_device_limit_is_enforced(repo, audit, clock, bus):
    service = PairingService(repo, bus=bus, audit=audit, max_devices=1, clock=clock)
    first = await service.start()
    await service.redeem(first.code, sender_id="111")
    second = await service.start()

    result = await service.redeem(second.code, sender_id="222")

    assert not result.ok and result.reason == "max_devices"


async def test_revocation_makes_the_sender_unknown_again(pairing, repo, bus):
    start = await pairing.start()
    paired = await pairing.redeem(start.code, sender_id=SENDER)

    assert await pairing.revoke(paired.device_id, reason="dashboard")

    assert repo.find_device_for_sender("telegram", SENDER) is None
    device = repo.get_device(paired.device_id)
    assert device is not None and device.revoked and device.revoked_reason == "dashboard"
    assert [e for e in bus.published if e.name == "remote.revoked"]


async def test_revoking_twice_is_a_no_op(pairing):
    start = await pairing.start()
    paired = await pairing.redeem(start.code, sender_id=SENDER)
    await pairing.revoke(paired.device_id)

    assert await pairing.revoke(paired.device_id) is False


async def test_a_revoked_device_can_pair_again_with_a_fresh_code(pairing, repo):
    first = await pairing.start()
    paired = await pairing.redeem(first.code, sender_id=SENDER)
    await pairing.revoke(paired.device_id)
    second = await pairing.start()

    again = await pairing.redeem(second.code, sender_id=SENDER)

    assert again.ok and again.device_id != paired.device_id


async def test_expired_unredeemed_codes_are_purged_on_the_next_start(pairing, db, clock):
    await pairing.start()
    clock.advance(301)

    await pairing.start()

    assert len(db.fetch_all("SELECT * FROM remote_pairings")) == 1


async def test_devices_listing_never_exposes_key_material(pairing, repo):
    start = await pairing.start()
    await pairing.redeem(start.code, sender_id=SENDER)

    listed = pairing.devices()

    assert listed and not hasattr(listed[0], "public_key")
    assert "public_key" not in listed[0].model_dump()
