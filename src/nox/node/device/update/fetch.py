"""Fetching a release: the project's release URL and a fetcher that speaks HTTPS only, follows no
redirect to another scheme and reads at most what it is told."""

from __future__ import annotations

import re
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from nox.release.verify import (
    ReleaseVerificationError,
)

RELEASES_URL = "https://github.com/Crackxsy/nox/releases/"


_PATH = re.compile(r"^(?:latest/download|download/v[0-9A-Za-z.-]{1,60})/[A-Za-z0-9._+-]{1,200}$")


class Fetcher(Protocol):
    def get(self, path: str, limit: int) -> bytes: ...
    def download(self, path: str, dest: Path, limit: int) -> None: ...


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """GitHub answers release downloads with a redirect to its file host; never to plain HTTP."""

    def redirect_request(  # noqa: PLR0913 - the standard library's signature
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not newurl.startswith("https://"):
            raise urllib.error.HTTPError(
                newurl, code, "refused: redirect away from HTTPS", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpsFetcher:
    """Release files over HTTPS with the system's certificate store, size-capped. Paths are
    `latest/download/<file>` or `download/<tag>/<file>` below the project's releases."""

    def __init__(self, base: str = RELEASES_URL, *, timeout_s: float = 60.0) -> None:
        if not base.startswith("https://"):
            raise ValueError("release files only come over HTTPS")
        self._base = base
        self._timeout = timeout_s
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), _HttpsOnly()
        )

    def _open(self, path: str) -> Any:
        if not _PATH.match(path):
            raise ValueError(f"not a release file path: {path[:80]!r}")
        return self._opener.open(self._base + path, timeout=self._timeout)

    def get(self, path: str, limit: int) -> bytes:
        with self._open(path) as response:
            data: bytes = response.read(limit + 1)
        if len(data) > limit:
            raise ReleaseVerificationError("too_large", f"{path} is larger than expected")
        return data

    def download(self, path: str, dest: Path, limit: int) -> None:
        written = 0
        with self._open(path) as response, dest.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise ReleaseVerificationError("too_large", f"{path} is larger than listed")
                out.write(chunk)
