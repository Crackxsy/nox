"""Windows Filtering Platform plumbing for `nox.sandbox.wfp`: add and remove Nox's filters.

Plain `ctypes` against `fwpuclnt.dll`, imported on Windows only and only by the installer's
elevated step (`python -m nox.sandbox.wfp install|remove`) - the Nox core never calls it, and
never needs an administrator. Structure layouts follow `fwptypes.h`/`fwpmtypes.h` of the Windows
SDK (x64 sizes are pinned in `tests/unit/sandbox/test_wfp.py`).

Everything happens in one transaction: either every filter is in place or none changed. Provider,
sublayer and filters are persistent (they survive a reboot and are enforced from the moment the
Base Filtering Engine starts), so nothing of Nox has to run for them to hold.
"""

from __future__ import annotations

import ctypes
import ipaddress
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from ctypes import wintypes
from typing import Any

from nox.sandbox.wfp import (
    PROVIDER_KEY,
    SUBLAYER_KEY,
    SUBLAYER_WEIGHT,
    Condition,
    FilterRule,
    Flags,
    Package,
    PortRange,
    Protocol,
    Remote,
    sid_bytes,
)


class GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    )


def guid(value: uuid.UUID) -> GUID:
    return GUID.from_buffer_copy(value.bytes_le)


def to_uuid(value: GUID) -> uuid.UUID:
    return uuid.UUID(bytes_le=bytes(value))


# fwptypes.h: FWP_DATA_TYPE, FWP_MATCH_TYPE, FWP_ACTION_*
FWP_EMPTY = 0
FWP_UINT8 = 1
FWP_UINT16 = 2
FWP_UINT32 = 3
FWP_SID = 13
FWP_V4_ADDR_MASK = 0x100
FWP_V6_ADDR_MASK = 0x101
FWP_RANGE_TYPE = 0x102
FWP_MATCH_EQUAL = 0
FWP_MATCH_RANGE = 5
FWP_MATCH_FLAGS_ANY_SET = 7
FWP_ACTION_BLOCK = 0x1001
FWP_ACTION_PERMIT = 0x1002
FWP_FILTER_ENUM_OVERLAPPING = 1
# fwpmtypes.h flags
FWPM_PROVIDER_FLAG_PERSISTENT = 0x1
FWPM_SUBLAYER_FLAG_PERSISTENT = 0x1
FWPM_FILTER_FLAG_PERSISTENT = 0x1
FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT = 0x8
RPC_C_AUTHN_WINNT = 10
# winerror.h
FWP_E_FILTER_NOT_FOUND = 0x80320003
FWP_E_PROVIDER_NOT_FOUND = 0x80320005
FWP_E_SUBLAYER_NOT_FOUND = 0x80320007
FWP_E_ALREADY_EXISTS = 0x80320009
ERROR_ACCESS_DENIED = 5

# fwpmu.h
LAYERS = {
    "ALE_AUTH_CONNECT_V4": uuid.UUID("c38d57d1-05a7-4c33-904f-7fbceee60e82"),
    "ALE_AUTH_CONNECT_V6": uuid.UUID("4a72393b-319f-44bc-84c3-ba54dcb3b6b4"),
}
CONDITION_PACKAGE_ID = uuid.UUID("71bc78fa-f17c-4997-a602-6abb261f351c")
CONDITION_REMOTE_ADDRESS = uuid.UUID("b235ae9a-1d64-49b8-a44c-5ff3d9095045")
CONDITION_REMOTE_PORT = uuid.UUID("c35a604d-d22b-4e1a-91b4-68f674ee674b")
CONDITION_PROTOCOL = uuid.UUID("3971ef2b-623e-4f9a-8cb1-6e79b806b9a7")
CONDITION_FLAGS = uuid.UUID("632ce23b-5167-435c-86d7-e903684aa80c")


class FWP_BYTE_BLOB(ctypes.Structure):  # noqa: N801 - the SDK's name
    _fields_ = (("size", ctypes.c_uint32), ("data", ctypes.c_void_p))


class FWPM_DISPLAY_DATA0(ctypes.Structure):  # noqa: N801
    _fields_ = (("name", ctypes.c_wchar_p), ("description", ctypes.c_wchar_p))


class FWP_V4_ADDR_AND_MASK(ctypes.Structure):  # noqa: N801
    _fields_ = (("addr", ctypes.c_uint32), ("mask", ctypes.c_uint32))


class FWP_V6_ADDR_AND_MASK(ctypes.Structure):  # noqa: N801
    _fields_ = (("addr", ctypes.c_ubyte * 16), ("prefixLength", ctypes.c_ubyte))


class _ValueUnion(ctypes.Union):
    # Every pointer member of the SDK union is one `void *` here; the scalars are listed.
    _fields_ = (
        ("uint8", ctypes.c_uint8),
        ("uint16", ctypes.c_uint16),
        ("uint32", ctypes.c_uint32),
        ("pointer", ctypes.c_void_p),
    )


class FWP_VALUE0(ctypes.Structure):  # noqa: N801
    _anonymous_ = ("u",)
    _fields_ = (("type", ctypes.c_int), ("u", _ValueUnion))


class FWP_CONDITION_VALUE0(ctypes.Structure):  # noqa: N801
    _anonymous_ = ("u",)
    _fields_ = (("type", ctypes.c_int), ("u", _ValueUnion))


class FWP_RANGE0(ctypes.Structure):  # noqa: N801
    _fields_ = (("valueLow", FWP_VALUE0), ("valueHigh", FWP_VALUE0))


class FWPM_FILTER_CONDITION0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("fieldKey", GUID),
        ("matchType", ctypes.c_int),
        ("conditionValue", FWP_CONDITION_VALUE0),
    )


class _ActionUnion(ctypes.Union):
    _fields_ = (("filterType", GUID), ("calloutKey", GUID), ("bitmapIndex", ctypes.c_uint8))


class FWPM_ACTION0(ctypes.Structure):  # noqa: N801
    _anonymous_ = ("u",)
    _fields_ = (("type", ctypes.c_uint32), ("u", _ActionUnion))


class _ContextUnion(ctypes.Union):
    _fields_ = (("rawContext", ctypes.c_uint64), ("providerContextKey", GUID))


class FWPM_FILTER0(ctypes.Structure):  # noqa: N801
    _anonymous_ = ("ctx",)
    _fields_ = (
        ("filterKey", GUID),
        ("displayData", FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("providerKey", ctypes.POINTER(GUID)),
        ("providerData", FWP_BYTE_BLOB),
        ("layerKey", GUID),
        ("subLayerKey", GUID),
        ("weight", FWP_VALUE0),
        ("numFilterConditions", ctypes.c_uint32),
        ("filterCondition", ctypes.POINTER(FWPM_FILTER_CONDITION0)),
        ("action", FWPM_ACTION0),
        ("ctx", _ContextUnion),
        ("reserved", ctypes.POINTER(GUID)),
        ("filterId", ctypes.c_uint64),
        ("effectiveWeight", FWP_VALUE0),
    )


class FWPM_PROVIDER0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("providerKey", GUID),
        ("displayData", FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("providerData", FWP_BYTE_BLOB),
        ("serviceName", ctypes.c_wchar_p),
    )


class FWPM_SUBLAYER0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("subLayerKey", GUID),
        ("displayData", FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("providerKey", ctypes.POINTER(GUID)),
        ("providerData", FWP_BYTE_BLOB),
        ("weight", ctypes.c_uint16),
    )


class FWPM_FILTER_ENUM_TEMPLATE0(ctypes.Structure):  # noqa: N801
    _fields_ = (
        ("providerKey", ctypes.POINTER(GUID)),
        ("layerKey", GUID),
        ("enumType", ctypes.c_int),
        ("flags", ctypes.c_uint32),
        ("providerContextTemplate", ctypes.c_void_p),
        ("numFilterConditions", ctypes.c_uint32),
        ("filterCondition", ctypes.POINTER(FWPM_FILTER_CONDITION0)),
        ("actionMask", ctypes.c_uint32),
        ("calloutKey", ctypes.POINTER(GUID)),
    )


class WfpError(OSError):
    def __init__(self, call: str, code: int) -> None:
        hint = " (run it as administrator)" if code == ERROR_ACCESS_DENIED else ""
        super().__init__(code, f"{call} failed with {code:#x}{hint}")
        self.code = code


class _Built:
    """A filter's ctypes structures plus everything they point at, kept alive together."""

    def __init__(self, rule: FilterRule, provider: GUID) -> None:
        self._keep: list[Any] = []
        conditions = (FWPM_FILTER_CONDITION0 * len(rule.conditions))()
        for slot, cond in zip(conditions, rule.conditions, strict=True):
            self._fill(slot, cond)
        f = FWPM_FILTER0()
        f.filterKey = guid(rule.key)
        f.displayData.name = rule.name
        f.displayData.description = "Keeps Nox's sandbox off loopback and the LAN"
        f.flags = FWPM_FILTER_FLAG_PERSISTENT | (
            FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT if rule.hard else 0
        )
        f.providerKey = ctypes.pointer(provider)
        f.layerKey = guid(LAYERS[rule.layer])
        f.subLayerKey = guid(SUBLAYER_KEY)
        f.weight.type = FWP_UINT8
        f.weight.uint8 = rule.weight
        f.numFilterConditions = len(rule.conditions)
        f.filterCondition = ctypes.cast(conditions, ctypes.POINTER(FWPM_FILTER_CONDITION0))
        f.action.type = FWP_ACTION_PERMIT if rule.action == "PERMIT" else FWP_ACTION_BLOCK
        self.filter = f
        self._keep += [conditions, provider]

    def _ptr(self, obj: Any) -> int:
        self._keep.append(obj)
        return ctypes.addressof(obj)

    def _fill(self, slot: FWPM_FILTER_CONDITION0, cond: Condition) -> None:
        value = slot.conditionValue
        slot.matchType = FWP_MATCH_EQUAL
        match cond:
            case Package(sid):
                slot.fieldKey = guid(CONDITION_PACKAGE_ID)
                value.type = FWP_SID
                value.pointer = self._ptr(ctypes.create_string_buffer(sid_bytes(sid)))
            case Remote(network):
                slot.fieldKey = guid(CONDITION_REMOTE_ADDRESS)
                if isinstance(network, ipaddress.IPv4Network):
                    # Host byte order, as WFP expects IPv4 addresses and masks.
                    v4 = FWP_V4_ADDR_AND_MASK(int(network.network_address), int(network.netmask))
                    value.type = FWP_V4_ADDR_MASK
                    value.pointer = self._ptr(v4)
                else:
                    v6 = FWP_V6_ADDR_AND_MASK()
                    ctypes.memmove(v6.addr, network.network_address.packed, 16)
                    v6.prefixLength = network.prefixlen
                    value.type = FWP_V6_ADDR_MASK
                    value.pointer = self._ptr(v6)
            case PortRange(low, high):
                slot.fieldKey = guid(CONDITION_REMOTE_PORT)
                slot.matchType = FWP_MATCH_RANGE
                rng = FWP_RANGE0()
                rng.valueLow.type = rng.valueHigh.type = FWP_UINT16
                rng.valueLow.uint16, rng.valueHigh.uint16 = low, high
                value.type = FWP_RANGE_TYPE
                value.pointer = self._ptr(rng)
            case Protocol(number):
                slot.fieldKey = guid(CONDITION_PROTOCOL)
                value.type = FWP_UINT8
                value.uint8 = number
            case Flags(mask):
                slot.fieldKey = guid(CONDITION_FLAGS)
                slot.matchType = FWP_MATCH_FLAGS_ANY_SET
                value.type = FWP_UINT32
                value.uint32 = mask


def _lib() -> Any:
    lib = ctypes.WinDLL("fwpuclnt")  # type: ignore[attr-defined,unused-ignore]
    handle_p = ctypes.POINTER(wintypes.HANDLE)
    sigs: dict[str, list[Any]] = {
        "FwpmEngineOpen0": [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            handle_p,
        ],
        "FwpmEngineClose0": [wintypes.HANDLE],
        "FwpmTransactionBegin0": [wintypes.HANDLE, ctypes.c_uint32],
        "FwpmTransactionCommit0": [wintypes.HANDLE],
        "FwpmTransactionAbort0": [wintypes.HANDLE],
        "FwpmProviderAdd0": [wintypes.HANDLE, ctypes.POINTER(FWPM_PROVIDER0), ctypes.c_void_p],
        "FwpmProviderDeleteByKey0": [wintypes.HANDLE, ctypes.POINTER(GUID)],
        "FwpmSubLayerAdd0": [wintypes.HANDLE, ctypes.POINTER(FWPM_SUBLAYER0), ctypes.c_void_p],
        "FwpmSubLayerDeleteByKey0": [wintypes.HANDLE, ctypes.POINTER(GUID)],
        "FwpmFilterAdd0": [
            wintypes.HANDLE,
            ctypes.POINTER(FWPM_FILTER0),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),
        ],
        "FwpmFilterDeleteByKey0": [wintypes.HANDLE, ctypes.POINTER(GUID)],
        "FwpmFilterCreateEnumHandle0": [
            wintypes.HANDLE,
            ctypes.POINTER(FWPM_FILTER_ENUM_TEMPLATE0),
            handle_p,
        ],
        "FwpmFilterEnum0": [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(FWPM_FILTER0))),
            ctypes.POINTER(ctypes.c_uint32),
        ],
        "FwpmFilterDestroyEnumHandle0": [wintypes.HANDLE, wintypes.HANDLE],
    }
    for name, args in sigs.items():
        fn = getattr(lib, name)
        fn.argtypes = args
        fn.restype = wintypes.DWORD
    lib.FwpmFreeMemory0.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.FwpmFreeMemory0.restype = None
    return lib


def _check(call: str, code: int, *ok: int) -> int:
    code &= 0xFFFFFFFF
    if code != 0 and code not in ok:
        raise WfpError(call, code)
    return code


@contextmanager
def _transaction() -> Iterator[tuple[Any, wintypes.HANDLE]]:
    lib = _lib()
    engine = wintypes.HANDLE()
    _check(
        "FwpmEngineOpen0",
        lib.FwpmEngineOpen0(None, RPC_C_AUTHN_WINNT, None, None, ctypes.byref(engine)),
    )
    try:
        _check("FwpmTransactionBegin0", lib.FwpmTransactionBegin0(engine, 0))
        try:
            yield lib, engine
        except BaseException:
            lib.FwpmTransactionAbort0(engine)
            raise
        _check("FwpmTransactionCommit0", lib.FwpmTransactionCommit0(engine))
    finally:
        lib.FwpmEngineClose0(engine)


def _provider_filter_keys(
    lib: Any, engine: wintypes.HANDLE, layers: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """The keys of every filter Nox's provider owns in `layers` (also ones an older build added)."""
    found: set[uuid.UUID] = set()
    provider = guid(PROVIDER_KEY)
    for layer in layers:
        template = FWPM_FILTER_ENUM_TEMPLATE0()
        template.providerKey = ctypes.pointer(provider)
        template.layerKey = guid(layer)
        template.enumType = FWP_FILTER_ENUM_OVERLAPPING
        template.actionMask = 0xFFFFFFFF
        handle = wintypes.HANDLE()
        _check(
            "FwpmFilterCreateEnumHandle0",
            lib.FwpmFilterCreateEnumHandle0(engine, ctypes.byref(template), ctypes.byref(handle)),
        )
        try:
            while True:
                entries = ctypes.POINTER(ctypes.POINTER(FWPM_FILTER0))()
                count = ctypes.c_uint32()
                _check(
                    "FwpmFilterEnum0",
                    lib.FwpmFilterEnum0(
                        engine, handle, 256, ctypes.byref(entries), ctypes.byref(count)
                    ),
                )
                try:
                    for i in range(count.value):
                        entry = entries[i].contents
                        owner = entry.providerKey
                        if owner and to_uuid(owner.contents) == PROVIDER_KEY:
                            found.add(to_uuid(entry.filterKey))
                finally:
                    if entries:
                        raw = ctypes.cast(entries, ctypes.c_void_p)
                        lib.FwpmFreeMemory0(ctypes.byref(raw))
                if count.value < 256:
                    break
        finally:
            lib.FwpmFilterDestroyEnumHandle0(engine, handle)
    return found


def install(rules: Sequence[FilterRule]) -> int:
    """Add provider, sublayer and every rule in one transaction, replacing what was there."""
    with _transaction() as (lib, engine):
        provider = FWPM_PROVIDER0()
        provider.providerKey = guid(PROVIDER_KEY)
        provider.displayData.name = "Nox"
        provider.displayData.description = "Network rules of Nox's command sandbox"
        provider.flags = FWPM_PROVIDER_FLAG_PERSISTENT
        _check(
            "FwpmProviderAdd0",
            lib.FwpmProviderAdd0(engine, ctypes.byref(provider), None),
            FWP_E_ALREADY_EXISTS,
        )
        provider_key = guid(PROVIDER_KEY)
        sublayer = FWPM_SUBLAYER0()
        sublayer.subLayerKey = guid(SUBLAYER_KEY)
        sublayer.displayData.name = "Nox sandbox"
        sublayer.displayData.description = "Keeps Nox's sandbox off loopback and the LAN"
        sublayer.flags = FWPM_SUBLAYER_FLAG_PERSISTENT
        sublayer.providerKey = ctypes.pointer(provider_key)
        sublayer.weight = SUBLAYER_WEIGHT
        _check(
            "FwpmSubLayerAdd0",
            lib.FwpmSubLayerAdd0(engine, ctypes.byref(sublayer), None),
            FWP_E_ALREADY_EXISTS,
        )
        # Replace, never duplicate: an upgrade may change a rule but keeps its key. Filters of
        # an older build that this one no longer has go too.
        stale = _provider_filter_keys(lib, engine, list(LAYERS.values()))
        for key in stale | {rule.key for rule in rules}:
            _check(
                "FwpmFilterDeleteByKey0",
                lib.FwpmFilterDeleteByKey0(engine, ctypes.byref(guid(key))),
                FWP_E_FILTER_NOT_FOUND,
            )
        for rule in rules:
            built = _Built(rule, guid(PROVIDER_KEY))
            _check(
                f"FwpmFilterAdd0({rule.name})",
                lib.FwpmFilterAdd0(engine, ctypes.byref(built.filter), None, None),
            )
    return len(rules)


def uninstall(rules: Sequence[FilterRule]) -> int:
    """Remove every filter of Nox's provider (known keys and any found), sublayer, provider."""
    with _transaction() as (lib, engine):
        keys = {rule.key for rule in rules}
        keys |= _provider_filter_keys(lib, engine, list(LAYERS.values()))
        removed = 0
        for key in keys:
            code = _check(
                "FwpmFilterDeleteByKey0",
                lib.FwpmFilterDeleteByKey0(engine, ctypes.byref(guid(key))),
                FWP_E_FILTER_NOT_FOUND,
            )
            removed += code == 0
        _check(
            "FwpmSubLayerDeleteByKey0",
            lib.FwpmSubLayerDeleteByKey0(engine, ctypes.byref(guid(SUBLAYER_KEY))),
            FWP_E_SUBLAYER_NOT_FOUND,
        )
        _check(
            "FwpmProviderDeleteByKey0",
            lib.FwpmProviderDeleteByKey0(engine, ctypes.byref(guid(PROVIDER_KEY))),
            FWP_E_PROVIDER_NOT_FOUND,
        )
    return removed
