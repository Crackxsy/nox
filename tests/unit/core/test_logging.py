"""nox.core.logging: secrets and PII never reach a sink (file or console)."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from nox.core.logging import (
    REDACTED,
    configure_logging,
    get_logger,
    is_secret_key,
    mask_string,
    pii_filter,
    shutdown_logging,
)

SECRET = "sk-live-9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # noqa: S105 - test fixture, not a real key
HEX = "0123456789abcdef0123456789abcdef0123456789abcdef"
B64 = "QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4OTA="


@pytest.fixture(autouse=True)
def _clean_logging() -> None:
    yield
    shutdown_logging()


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "TOKEN",
        "secret",
        "client_secret",
        "password",
        "api_key",
        "apikey",
        "Authorization",
        "pin",
        "pin_code",
        "credential",
    ],
)
def test_secret_keys_detected(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize("key", ["event", "pinned", "spinner", "component", "tokens_in", "pinball"])
def test_non_secret_keys_pass(key: str) -> None:
    assert not is_secret_key(key)


def test_mask_string_masks_email_hex_base64_but_keeps_words() -> None:
    text = f"user alex.doe@example.org sent {HEX} and {B64} while configuration_loaded"
    masked = mask_string(text)
    assert "alex" not in masked and "[email]" in masked
    assert HEX not in masked and "[hex]" in masked
    assert B64 not in masked and "[b64]" in masked
    assert "configuration_loaded" in masked


def test_pii_filter_redacts_nested_structures() -> None:
    event = {
        "event": "provider.auth",
        "token": SECRET,
        "nested": {
            "api_key": SECRET,
            "items": [{"password": SECRET}, "mail me@x.de"],
            "ok": "fine",
        },
        "count": 3,
    }
    out = pii_filter(None, "info", event)
    assert out["token"] == REDACTED
    assert out["nested"]["api_key"] == REDACTED
    assert out["nested"]["items"][0]["password"] == REDACTED
    assert out["nested"]["items"][1] == "mail [email]"
    assert out["nested"]["ok"] == "fine"
    assert out["count"] == 3
    assert out["event"] == "provider.auth"


def test_secrets_never_reach_file_or_console(tmp_path: Path) -> None:
    console = io.StringIO()
    handlers = configure_logging(tmp_path, level="DEBUG", console_stream=console, retention_days=3)
    log = get_logger("nox.test")
    log.info("provider.connected", token=SECRET, email="alex@example.org", note=f"key {HEX}")
    log.warning(f"raw message with {SECRET} inside", authorization="Bearer abc")
    logging.getLogger("third.party").info("foreign logger mail me@example.org token=%s", SECRET)
    for handler in handlers:
        handler.flush()
    shutdown_logging()

    file_text = (tmp_path / "nox.log").read_text(encoding="utf-8")
    console_text = console.getvalue()
    for sink in (file_text, console_text):
        assert SECRET not in sink
        assert HEX not in sink
        assert "example.org" not in sink
        assert REDACTED in sink
    records = [json.loads(line) for line in file_text.splitlines() if line.strip()]
    assert any(r.get("event") == "provider.connected" and r["token"] == REDACTED for r in records)
    assert all("timestamp" in r and "level" in r for r in records)
    assert any(r["logger"] == "third.party" and "[email]" in r["event"] for r in records)


def test_console_only_when_logs_dir_none() -> None:
    console = io.StringIO()
    handlers = configure_logging(None, console_stream=console)
    assert len(handlers) == 1
    get_logger("x").info("hello", who="there")
    assert "hello" in console.getvalue()


def test_level_filtering(tmp_path: Path) -> None:
    console = io.StringIO()
    configure_logging(None, level="WARNING", console_stream=console)
    get_logger("x").info("invisible")
    get_logger("x").warning("visible")
    assert "invisible" not in console.getvalue()
    assert "visible" in console.getvalue()
