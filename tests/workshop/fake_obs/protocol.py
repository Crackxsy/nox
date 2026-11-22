"""The obs-websocket v5 wire protocol, written out from the specification: op codes, close
codes, event subscription bits, request status codes and the authentication hash.

Nothing here is imported from `nox`: the contract tests compare Nox's client with this, and a
constant copied from the client would agree with it by construction."""

from __future__ import annotations

import base64
import hashlib
import uuid

OP_HELLO = 0
OP_IDENTIFY = 1
OP_IDENTIFIED = 2
OP_REIDENTIFY = 3
OP_EVENT = 5
OP_REQUEST = 6
OP_REQUEST_RESPONSE = 7

RPC_VERSION = 1
OBS_WEBSOCKET_VERSION = "5.5.4"
OBS_VERSION = "30.2.3"

CLOSE_MESSAGE_DECODE_ERROR = 4002
CLOSE_UNKNOWN_OP_CODE = 4006
CLOSE_NOT_IDENTIFIED = 4007
CLOSE_ALREADY_IDENTIFIED = 4008
CLOSE_AUTHENTICATION_FAILED = 4009
CLOSE_UNSUPPORTED_RPC_VERSION = 4010

# Event subscription bits (`EventSubscription`).
SUB_GENERAL = 1 << 0
SUB_CONFIG = 1 << 1
SUB_SCENES = 1 << 2
SUB_INPUTS = 1 << 3
SUB_TRANSITIONS = 1 << 4
SUB_FILTERS = 1 << 5
SUB_OUTPUTS = 1 << 6
SUB_SCENE_ITEMS = 1 << 7
SUB_MEDIA_INPUTS = 1 << 8
SUB_VENDORS = 1 << 9
SUB_UI = 1 << 10
#: Everything except the high-volume categories, which real OBS also leaves out of `All`.
SUB_ALL = (1 << 11) - 1

#: Which category an event belongs to. An event not listed here is sent to everyone who is
#: identified (a scripted `emit` of an unknown type is the test's own business).
EVENT_CATEGORY: dict[str, int] = {
    "ExitStarted": SUB_GENERAL,
    "CurrentProgramSceneChanged": SUB_SCENES,
    "CurrentPreviewSceneChanged": SUB_SCENES,
    "SceneCreated": SUB_SCENES,
    "SceneRemoved": SUB_SCENES,
    "SceneNameChanged": SUB_SCENES,
    "SceneListChanged": SUB_SCENES,
    "InputCreated": SUB_INPUTS,
    "InputRemoved": SUB_INPUTS,
    "InputMuteStateChanged": SUB_INPUTS,
    "InputSettingsChanged": SUB_INPUTS,
    "SourceFilterCreated": SUB_FILTERS,
    "SourceFilterRemoved": SUB_FILTERS,
    "SourceFilterEnableStateChanged": SUB_FILTERS,
    "SceneItemCreated": SUB_SCENE_ITEMS,
    "SceneItemEnableStateChanged": SUB_SCENE_ITEMS,
    "StreamStateChanged": SUB_OUTPUTS,
    "RecordStateChanged": SUB_OUTPUTS,
    "ReplayBufferSaved": SUB_OUTPUTS,
    "VendorEvent": SUB_VENDORS,
}


class Status:
    """`RequestStatus` codes this fake answers with."""

    SUCCESS = 100
    MISSING_REQUEST_TYPE = 203
    UNKNOWN_REQUEST_TYPE = 204
    GENERIC_ERROR = 205
    MISSING_REQUEST_FIELD = 300
    INVALID_REQUEST_FIELD_TYPE = 401
    REQUEST_FIELD_EMPTY = 403
    OUTPUT_RUNNING = 500
    OUTPUT_NOT_RUNNING = 501
    RESOURCE_NOT_FOUND = 600
    RESOURCE_ALREADY_EXISTS = 601
    NOT_ENOUGH_RESOURCES = 603
    INVALID_RESOURCE_STATE = 604
    INVALID_INPUT_KIND = 605
    INVALID_FILTER_KIND = 607
    REQUEST_PROCESSING_FAILED = 702


def auth_string(password: str, salt: str, challenge: str) -> str:
    """base64(sha256(base64(sha256(password + salt)) + challenge)), straight from the protocol."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def scene_uuid(name: str) -> str:
    """A stable id per scene name, so a test can compare ids across requests."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://example.invalid/scene/{name}"))


class ObsRequestError(Exception):
    """Raised inside a request handler to answer `result: false` with this code and comment."""

    def __init__(self, code: int, comment: str) -> None:
        super().__init__(comment)
        self.code = code
        self.comment = comment
