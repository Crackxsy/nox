"""Pairing and recognising devices (`nox.lan.devices`), and that the phone companion's devices
and the network's never mix although they share one table."""

from __future__ import annotations

import pytest

from nox.data.db import Database
from nox.ipc.tokens import TokenStore
from nox.lan.devices import CHANNEL, LanDevices
from nox.remote.pairing import PairingService
from nox.remote.repo import RemoteRepository


async def test_a_code_yields_a_device_and_its_token(devices: LanDevices) -> None:
    start = await devices.start(name="Living room")
    redeemed = await devices.redeem(start.code, name="Sam's phone")
    assert redeemed.result.ok
    assert redeemed.token is not None and len(redeemed.token) >= 40
    device = devices.verify(redeemed.token)
    assert device is not None
    assert device.name == "Sam's phone"
    assert device.channel == CHANNEL and device.role == "device"
    assert device.last_seen_at is not None


async def test_only_a_hash_of_the_token_is_stored(devices: LanDevices, db: Database) -> None:
    start = await devices.start()
    redeemed = await devices.redeem(start.code)
    assert redeemed.token is not None
    for table in ("paired_devices", "remote_pairings"):
        for row in db.fetch_all(f"SELECT * FROM {table}"):  # noqa: S608 - fixed table names
            for value in tuple(row):
                assert redeemed.token not in str(value)
                assert start.code not in str(value)


async def test_a_code_works_once(devices: LanDevices) -> None:
    start = await devices.start()
    assert (await devices.redeem(start.code)).result.ok
    again = await devices.redeem(start.code)
    assert not again.result.ok and again.result.reason == "used"
    assert again.token is None


async def test_a_wrong_code_gets_nothing(devices: LanDevices) -> None:
    result = await devices.redeem("ZZZZZZZZ")
    assert not result.result.ok and result.token is None


async def test_a_revoked_device_is_not_recognised(devices: LanDevices) -> None:
    start = await devices.start()
    redeemed = await devices.redeem(start.code)
    assert redeemed.token is not None
    device = devices.verify(redeemed.token)
    assert device is not None
    assert await devices.revoke(device.id)
    assert devices.verify(redeemed.token) is None


async def test_the_device_limit_counts_network_devices_only(
    repo: RemoteRepository, devices: LanDevices
) -> None:
    telegram = PairingService(repo, max_devices=1)
    phone = await telegram.start()
    assert (await telegram.redeem(phone.code, sender_id="4711")).ok
    # Three network devices fit next to the Telegram phone (limit 3)...
    for _ in range(3):
        assert (await devices.redeem((await devices.start()).code)).result.ok
    # ...a fourth does not, and the phone's own limit is unaffected by them.
    fourth = await devices.redeem((await devices.start()).code)
    assert fourth.result.reason == "max_devices"
    assert [d.channel for d in telegram.devices()] == ["telegram"]
    assert len(devices.devices()) == 3


async def test_one_channel_cannot_revoke_the_others_device(
    repo: RemoteRepository, devices: LanDevices
) -> None:
    telegram = PairingService(repo)
    redeemed = await devices.redeem((await devices.start()).code)
    assert redeemed.token is not None
    device = devices.verify(redeemed.token)
    assert device is not None
    assert not await telegram.revoke(device.id)
    assert devices.verify(redeemed.token) is not None


# ---- the hub's token store -------------------------------------------------------------------


@pytest.fixture
def store() -> TokenStore:
    tokens = TokenStore()
    tokens.use_device_verifier(lambda token: "abc123" if token == "good-token" else None)
    return tokens


def test_a_paired_device_authenticates_over_the_network(store: TokenStore) -> None:
    decision = store.authenticate("good-token", "device", "device:abc123", via_lan=True)
    assert decision.ok and decision.kind == "device" and decision.device_id == "abc123"


def test_nothing_but_a_device_comes_in_over_the_network(store: TokenStore) -> None:
    """Not even with the right session token: the network can never pose as the shell."""
    for role in ("shell", "dashboard", "pet", "worker", "plugin"):
        decision = store.authenticate(store.session_token, role, f"{role}:x", via_lan=True)
        assert not decision.ok, role


def test_a_device_never_comes_in_locally(store: TokenStore) -> None:
    assert not store.authenticate("good-token", "device", "device:abc123").ok


def test_a_token_is_bound_to_its_device(store: TokenStore) -> None:
    assert not store.authenticate("good-token", "device", "device:other", via_lan=True).ok
    assert not store.authenticate("bad-token", "device", "device:abc123", via_lan=True).ok


def test_without_network_access_no_device_authenticates() -> None:
    tokens = TokenStore()
    decision = tokens.authenticate("good-token", "device", "device:abc123", via_lan=True)
    assert not decision.ok and "off" in decision.reason
