"""The listener over time (`nox.lan.install.LanListener`): start, re-check, reload, and what the
health check says in each state. Certificates and servers are stand-ins; nothing listens."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from nox.core.events import HealthStatus
from nox.lan.devices import LanDevices
from nox.lan.install import LanListener, LanRuntime, _add_health_check
from nox.lan.tls import KeyNotProtectedError, TlsMaterial


def material(serial: int) -> TlsMaterial:
    return TlsMaterial(
        cert_file=Path("server.crt"),
        key_file=Path("server.key"),
        ca_file=Path("ca.crt"),
        ca_der=b"",
        fingerprint="AA:BB",
        names=("localhost",),
        addresses=("127.0.0.1", "192.168.1.20"),
        server_serial=serial,
    )


class FakeServer:
    def __init__(self, serial: int, *, fail: bool = False) -> None:
        self.serial = serial
        self.fail = fail
        self.started = False
        self.stopped = False

    @property
    def port(self) -> int:
        return 50000 + self.serial

    async def start(self) -> None:
        if self.fail:
            raise OSError("address already in use")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class Certificates:
    """Hands out the next prepared answer: material, or an exception to raise."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)

    def __call__(self) -> TlsMaterial:
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            return answer()
        return answer


@pytest.fixture
def runtime(devices: LanDevices) -> LanRuntime:
    return LanRuntime(devices=devices, enabled=True, port=0)


def listener(
    runtime: LanRuntime, certs: Certificates, built: list[FakeServer], *, fail_on: int = -1
) -> LanListener:
    def build(m: TlsMaterial) -> FakeServer:
        server = FakeServer(m.server_serial, fail=m.server_serial == fail_on)
        built.append(server)
        return server

    return LanListener(runtime, make_tls=certs, build=build, timeout_s=0.2)


async def test_starts_and_reports_the_port(runtime: LanRuntime) -> None:
    built: list[FakeServer] = []
    assert await listener(runtime, Certificates(material(1)), built).start()
    assert runtime.running and built[0].started
    assert runtime.port == 50001
    assert runtime.urls() == ["https://192.168.1.20:50001/dashboard/"]


@pytest.mark.parametrize(
    "failure",
    [
        KeyNotProtectedError("ca.key could not be made owner-only"),
        OSError("resolver"),
        ValueError("bad pem"),
    ],
    ids=["key-unprotected", "os-error", "bad-pem"],
)
async def test_no_certificate_means_no_listener(runtime: LanRuntime, failure: Exception) -> None:
    built: list[FakeServer] = []
    assert not await listener(runtime, Certificates(failure), built).start()
    assert built == []  # fail closed: nothing was even built
    assert not runtime.running
    assert runtime.error.startswith("certificates:") and type(failure).__name__ in runtime.error


async def test_a_hanging_resolver_cannot_hold_the_start(runtime: LanRuntime) -> None:
    def slow() -> TlsMaterial:
        time.sleep(1.0)
        return material(1)

    built: list[FakeServer] = []
    started = time.monotonic()
    assert not await listener(runtime, Certificates(slow), built).start()
    assert time.monotonic() - started < 0.9
    assert "TimeoutError" in runtime.error and built == []


async def test_a_port_in_use_is_reported(runtime: LanRuntime) -> None:
    built: list[FakeServer] = []
    assert not await listener(runtime, Certificates(material(1)), built, fail_on=1).start()
    assert not runtime.running and "address already in use" in runtime.error


async def test_the_same_certificate_keeps_the_listener(runtime: LanRuntime) -> None:
    built: list[FakeServer] = []
    lan = listener(runtime, Certificates(material(1), material(1)), built)
    await lan.start()
    await lan.recheck()
    assert len(built) == 1 and not built[0].stopped


async def test_a_new_certificate_restarts_the_listener(runtime: LanRuntime) -> None:
    """A new serial - new addresses, a renamed machine, the end of validity near - is only served
    by a restarted listener."""
    built: list[FakeServer] = []
    lan = listener(runtime, Certificates(material(1), material(2)), built)
    await lan.start()
    await lan.recheck()
    assert [s.serial for s in built] == [1, 2]
    assert built[0].stopped and built[1].started
    assert runtime.server is built[1] and runtime.tls is not None and runtime.tls.server_serial == 2


async def test_a_failed_check_while_serving_is_a_warning_that_clears(runtime: LanRuntime) -> None:
    built: list[FakeServer] = []
    lan = listener(runtime, Certificates(material(1), OSError("resolver down"), material(1)), built)
    await lan.start()
    await lan.recheck()
    assert runtime.running and runtime.error == ""
    assert "resolver down" in runtime.warning
    await lan.recheck()
    assert runtime.warning == "" and runtime.running


async def test_a_listener_that_never_started_comes_up_on_a_later_check(runtime: LanRuntime) -> None:
    built: list[FakeServer] = []
    lan = listener(runtime, Certificates(OSError("resolver down"), material(1)), built)
    assert not await lan.start()
    await lan.recheck()
    assert runtime.running and runtime.error == ""


# ---- the health check ------------------------------------------------------------------------


class FakeHealth:
    def __init__(self) -> None:
        self.checks: dict[str, Any] = {}

    def add_check(self, check: Any) -> None:
        self.checks[check.name] = check


class FakeCore:
    def __init__(self) -> None:
        self.health = FakeHealth()


async def probe(runtime: LanRuntime) -> tuple[HealthStatus, str]:
    core = FakeCore()
    _add_health_check(core, runtime)
    check = core.health.checks["lan"]
    return await check.probe()


async def test_health_follows_the_listener(runtime: LanRuntime) -> None:
    assert (await probe(runtime))[0] is HealthStatus.LIMITED  # starting
    runtime.error = "certificates: OSError: x"
    assert (await probe(runtime)) == (HealthStatus.UNAVAILABLE, "certificates: OSError: x")
    runtime.server, runtime.error = FakeServer(1), ""
    assert (await probe(runtime))[0] is HealthStatus.AVAILABLE
    runtime.warning = "certificates: OSError: resolver down"
    status, detail = await probe(runtime)
    assert status is HealthStatus.LIMITED and "resolver down" in detail
