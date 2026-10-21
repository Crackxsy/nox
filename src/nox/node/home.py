"""Where a node keeps what it is: its identity, its key and certificate, the pinned authority, its
capability manifest and its own audit trail - all in one folder of the logged-in user.

Default folder: `%LOCALAPPDATA%\\Nox\\node` on Windows, `~/Library/Application Support/Nox/node`
on macOS, `$XDG_DATA_HOME/nox/node` (`~/.local/share/nox/node`) elsewhere; `NOX_NODE_HOME`
overrides it. The folder is owner-only on POSIX (0700); the key file is owner-only everywhere.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["HOME_ENV", "NodeHome", "NodeIdentity", "default_home"]

HOME_ENV = "NOX_NODE_HOME"


def restrict_dir_to_owner(path: Path) -> bool:
    """Windows: the folder - and, inherited, everything in it - for this user alone. Inherited
    permissions (other users of the PC) are removed; True when `icacls` succeeded."""
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    if not user:
        return False
    account = f"{domain}\\{user}" if domain else user
    icacls = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "icacls.exe"
    result = subprocess.run(  # noqa: S603 - fixed program, arguments are data
        [str(icacls), str(path), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"],
        capture_output=True,
        check=False,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    return result.returncode == 0


def default_home() -> Path:
    override = os.environ.get(HOME_ENV, "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Nox" / "node"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Nox" / "node"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "nox" / "node"


class NodeIdentity(BaseModel):
    """What pairing gave this node. No secret: the key is in `node.key` (encrypted when the OS
    has a keyring)."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    name: str = Field(max_length=64)
    core_host: str = Field(max_length=253)
    core_port: int = Field(ge=1, le=65535)
    node_port: int = Field(ge=1, le=65535)
    #: SHA-256 of the authority, 64 hex digits: the pin. Another authority is never accepted.
    ca_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    paired_at: datetime


class NodeHome:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_home()

    identity_file = property(lambda self: self.path / "node.json")
    cert_file = property(lambda self: self.path / "node.crt")
    ca_file = property(lambda self: self.path / "ca.crt")
    key_file = property(lambda self: self.path / "node.key")
    manifest_file = property(lambda self: self.path / "node.yaml")
    audit_file = property(lambda self: self.path / "audit.db")
    status_file = property(lambda self: self.path / "status.json")

    def ensure(self) -> None:
        created = not self.path.exists()
        self.path.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(self.path, 0o700)
        elif created:
            restrict_dir_to_owner(self.path)

    # -- identity -------------------------------------------------------------------------------

    @property
    def paired(self) -> bool:
        files: tuple[Path, ...] = (self.identity_file, self.cert_file, self.key_file)
        return all(f.is_file() for f in files)

    def load_identity(self) -> NodeIdentity | None:
        try:
            return NodeIdentity.model_validate_json(self.identity_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save_pairing(self, identity: NodeIdentity, *, cert_pem: str, ca_pem: str) -> None:
        self.ensure()
        self.cert_file.write_text(cert_pem, encoding="utf-8")
        self.ca_file.write_text(ca_pem, encoding="utf-8")
        self.identity_file.write_text(identity.model_dump_json(indent=2), encoding="utf-8")

    def pinned_ca_ok(self, identity: NodeIdentity) -> bool:
        """The stored authority is still the one the pairing link pinned."""
        try:
            ca = x509.load_pem_x509_certificate(self.ca_file.read_bytes())
        except (OSError, ValueError):
            return False
        digest = hashlib.sha256(ca.public_bytes(serialization.Encoding.DER)).hexdigest()
        return digest == identity.ca_fingerprint

    def forget(self) -> None:
        """Removed from Nox: the key, the certificate and the identity go. The audit stays."""
        for file in (self.key_file, self.cert_file, self.identity_file, self.ca_file):
            with contextlib.suppress(FileNotFoundError):
                file.unlink()

    # -- status (what `nox node status` shows about a running node) -----------------------------

    def write_status(self, status: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            self.ensure()
            self.status_file.write_text(json.dumps(status, indent=2), encoding="utf-8")

    def read_status(self) -> dict[str, Any]:
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
