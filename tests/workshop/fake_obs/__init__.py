"""A fake OBS Studio (obs-websocket v5) with a small stateful OBS behind it; see `server.py`."""

from .protocol import ObsRequestError, Status, auth_string
from .server import FakeObs, IdentifyRecord, RecordedRequest
from .state import ObsState

__all__ = [
    "FakeObs",
    "IdentifyRecord",
    "ObsRequestError",
    "ObsState",
    "RecordedRequest",
    "Status",
    "auth_string",
]
