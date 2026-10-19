"""The redaction is linear: adversarial strings without whitespace cannot stall it."""

from __future__ import annotations

import time

import pytest

from nox.stream.obs_requests import MAX_STRING_CHARS, TRUNCATED, redact

KIB_100 = 100 * 1024

ADVERSARIAL = [
    "a" * KIB_100 + "://",
    "/" * KIB_100,
    "\\" * KIB_100,
    "a/" * (KIB_100 // 2),
    "x://" * (KIB_100 // 4),
    ("A1" * 15 + "/") * (KIB_100 // 31),
    "https://" + "h" * KIB_100,
    ":" * KIB_100 + "//",
]


@pytest.mark.parametrize("value", ADVERSARIAL, ids=range(len(ADVERSARIAL)))
def test_100_kib_of_adversarial_input_takes_under_50_ms(value: str) -> None:
    timings = []
    for _ in range(3):  # the best of three: the scheduler's noise is not the redaction's cost
        started = time.perf_counter()
        out = redact({"comment": value, "inputSettings": {"text": value, "url": value}})
        timings.append(time.perf_counter() - started)
    assert min(timings) < 0.05, f"{min(timings) * 1000:.1f} ms"
    assert out["comment"].endswith(TRUNCATED)
    assert len(out["comment"]) <= MAX_STRING_CHARS + 1 + len(TRUNCATED)


def test_the_cap_keeps_short_strings_whole() -> None:
    assert redact({"comment": "scene not found"})["comment"] == "scene not found"
