"""scripts/secrets_scan.py tells OpenSSH's key type names from API keys."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "secrets_scan.py"
spec = importlib.util.spec_from_file_location("secrets_scan_patterns", SCRIPT)
assert spec is not None and spec.loader is not None
scan = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scan
spec.loader.exec_module(scan)

# Assembled at runtime so this file itself is not a finding.
API_KEY = "sk-" + "live" + "-" + "Zq8vT2mK9" + "wL4xR7nP3cY6bJ1"


def labels(line: str) -> list[str]:
    return [label for label, _ in scan._scan_line(line)]  # noqa: SLF001


def test_openssh_key_type_names_are_no_api_keys() -> None:
    for name in ("sk-ecdsa-sha2-nistp256@openssh.com", "sk-ssh-ed25519@openssh.com"):
        assert "anthropic_or_openai_key" not in labels(f'    "{name}": None,')
        assert "anthropic_or_openai_key" not in labels(
            f"PubkeyAcceptedAlgorithms ssh-ed25519,{name}"
        )


def test_an_api_key_is_still_found() -> None:
    assert "anthropic_or_openai_key" in labels(f'key = "{API_KEY}"')
    # Dressed up like the protocol name, without being one.
    assert "anthropic_or_openai_key" in labels(f"{API_KEY}@openssh.com")
    disguised = "sk-" + "ecdsa-" + "Zq8vT2mK9" + "wL4xR7nP3cY6bJ1"
    assert "anthropic_or_openai_key" in labels(disguised)
