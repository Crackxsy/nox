"""Win32 plumbing for the Windows sandbox: a low-integrity token, a limited job, a hidden process.

Only `nox.sandbox.windows` imports this, and only on Windows. Everything is plain `ctypes` against
kernel32/advapi32, so there is no extra dependency and nothing that a reviewer cannot read.

What a child started by `spawn()` gets:

* a **primary token at low integrity** (S-1-16-4096), duplicated from the core's own token with
  every privilege removed (`DISABLE_MAX_PRIVILEGE`). Windows' mandatory integrity control then
  refuses it write access to everything labelled medium - which is every file the user owns -
  except folders explicitly labelled low (`label_low`);
* membership in a **job object** that kills the whole tree when the job handle closes, caps memory
  and the number of processes, and blocks the clipboard, global atoms, desktop switching and
  system-parameter changes (`JOB_OBJECT_UILIMIT_*`);
* no console window (`CREATE_NO_WINDOW`), and `SW_HIDE` for any window it would show;
* exactly the environment block it is given, and only its three standard handles - an explicit
  `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, so no other inheritable handle of the core leaks in.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from dataclasses import dataclass

# The module is imported on Windows only; these attributes do not exist elsewhere.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
_adv = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL

TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100
MAXIMUM_ALLOWED = 0x02000000
SECURITY_IMPERSONATION = 2
TOKEN_PRIMARY = 1
TOKEN_INTEGRITY_LEVEL = 25
SE_GROUP_INTEGRITY = 0x00000020
DISABLE_MAX_PRIVILEGE = 0x1

CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
STARTF_USESTDHANDLES = 0x00000100
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0
HANDLE_FLAG_INHERIT = 0x00000001
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
INFINITE = 0xFFFFFFFF
STILL_ACTIVE = 259

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_UI_RESTRICTIONS = 4
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
#: Every UI restriction except the handle one (which would also cut the child off from its own
#: console host): no clipboard, no global atoms, no desktop switching, no display or system
#: parameter changes, no ExitWindows.
JOB_UI_RESTRICTIONS = 0x0001 | 0x0002 | 0x0004 | 0x0008 | 0x0020 | 0x0040 | 0x0080

LOW_INTEGRITY_SID = "S-1-16-4096"
#: SDDL for "labelled low, inherited by files and folders": what a low-integrity process may write.
LOW_LABEL_SDDL = "S:(ML;OICI;NW;;;LW)"
SE_FILE_OBJECT = 1
LABEL_SECURITY_INFORMATION = 0x00000010
SDDL_REVISION_1 = 1


class SandboxWin32Error(OSError):
    """A Win32 call the sandbox depends on failed; the run does not happen."""


def _fail(what: str) -> SandboxWin32Error:
    code = ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
    return SandboxWin32Error(code, f"{what} failed: {ctypes.FormatError(code).strip()}")  # type: ignore[attr-defined,unused-ignore]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", DWORD)]


class _TokenMandatoryLabel(ctypes.Structure):
    _fields_ = [("Label", _SidAndAttributes)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", DWORD),
        ("dwY", DWORD),
        ("dwXSize", DWORD),
        ("dwYSize", DWORD),
        ("dwXCountChars", DWORD),
        ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", DWORD),
        ("dwThreadId", DWORD),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", BOOL),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("r", "w", "o", "rt", "wt", "ot")]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _UiRestrictions(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", DWORD)]


def _prototypes() -> None:
    _adv.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    _adv.OpenProcessToken.restype = BOOL
    _adv.DuplicateTokenEx.argtypes = [
        HANDLE,
        DWORD,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(HANDLE),
    ]
    _adv.DuplicateTokenEx.restype = BOOL
    _adv.CreateRestrictedToken.argtypes = [
        HANDLE,
        DWORD,
        DWORD,
        ctypes.c_void_p,
        DWORD,
        ctypes.c_void_p,
        DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(HANDLE),
    ]
    _adv.CreateRestrictedToken.restype = BOOL
    _adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _adv.ConvertStringSidToSidW.restype = BOOL
    _adv.SetTokenInformation.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD]
    _adv.SetTokenInformation.restype = BOOL
    _adv.GetLengthSid.argtypes = [ctypes.c_void_p]
    _adv.GetLengthSid.restype = DWORD
    _adv.CreateProcessAsUserW.argtypes = [
        HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        BOOL,
        DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessInformation),
    ]
    _adv.CreateProcessAsUserW.restype = BOOL
    _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(DWORD),
    ]
    _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = BOOL
    _adv.GetSecurityDescriptorSacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(BOOL),
    ]
    _adv.GetSecurityDescriptorSacl.restype = BOOL
    _adv.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _adv.SetNamedSecurityInfoW.restype = DWORD
    _k32.GetCurrentProcess.restype = HANDLE
    _k32.CloseHandle.argtypes = [HANDLE]
    _k32.CloseHandle.restype = BOOL
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _k32.LocalFree.restype = ctypes.c_void_p
    _k32.CreatePipe.argtypes = [
        ctypes.POINTER(HANDLE),
        ctypes.POINTER(HANDLE),
        ctypes.POINTER(_SecurityAttributes),
        DWORD,
    ]
    _k32.CreatePipe.restype = BOOL
    _k32.SetHandleInformation.argtypes = [HANDLE, DWORD, DWORD]
    _k32.SetHandleInformation.restype = BOOL
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = HANDLE
    _k32.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD]
    _k32.SetInformationJobObject.restype = BOOL
    _k32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
    _k32.AssignProcessToJobObject.restype = BOOL
    _k32.TerminateJobObject.argtypes = [HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = BOOL
    _k32.ResumeThread.argtypes = [HANDLE]
    _k32.ResumeThread.restype = DWORD
    _k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
    _k32.WaitForSingleObject.restype = DWORD
    _k32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
    _k32.GetExitCodeProcess.restype = BOOL
    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        DWORD,
        DWORD,
        ctypes.POINTER(_SecurityAttributes),
        DWORD,
        DWORD,
        HANDLE,
    ]
    _k32.CreateFileW.restype = HANDLE


_prototypes()


def low_integrity_token() -> int:
    """A primary token: the core's own, with every privilege removed, at low integrity."""
    own = HANDLE()
    access = (
        TOKEN_DUPLICATE
        | TOKEN_QUERY
        | TOKEN_ASSIGN_PRIMARY
        | TOKEN_ADJUST_DEFAULT
        | TOKEN_ADJUST_SESSIONID
    )
    if not _adv.OpenProcessToken(_k32.GetCurrentProcess(), access, ctypes.byref(own)):
        raise _fail("OpenProcessToken")
    restricted = HANDLE()
    try:
        if not _adv.CreateRestrictedToken(
            own, DISABLE_MAX_PRIVILEGE, 0, None, 0, None, 0, None, ctypes.byref(restricted)
        ):
            raise _fail("CreateRestrictedToken")
    finally:
        _k32.CloseHandle(own)
    primary = HANDLE()
    try:
        if not _adv.DuplicateTokenEx(
            restricted,
            MAXIMUM_ALLOWED,
            None,
            SECURITY_IMPERSONATION,
            TOKEN_PRIMARY,
            ctypes.byref(primary),
        ):
            raise _fail("DuplicateTokenEx")
    finally:
        _k32.CloseHandle(restricted)
    sid = ctypes.c_void_p()
    if not _adv.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid)):
        _k32.CloseHandle(primary)
        raise _fail("ConvertStringSidToSidW")
    try:
        label = _TokenMandatoryLabel(_SidAndAttributes(sid, SE_GROUP_INTEGRITY))
        size = ctypes.sizeof(label) + _adv.GetLengthSid(sid)
        if not _adv.SetTokenInformation(primary, TOKEN_INTEGRITY_LEVEL, ctypes.byref(label), size):
            _k32.CloseHandle(primary)
            raise _fail("SetTokenInformation(IntegrityLevel)")
    finally:
        _k32.LocalFree(sid)
    return int(primary.value or 0)


def label_low(path: str) -> None:
    """Label a folder (and, by inheritance, everything created in it) low integrity."""
    sd = ctypes.c_void_p()
    if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        LOW_LABEL_SDDL, SDDL_REVISION_1, ctypes.byref(sd), None
    ):
        raise _fail("ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        present = BOOL()
        defaulted = BOOL()
        sacl = ctypes.c_void_p()
        if not _adv.GetSecurityDescriptorSacl(
            sd, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)
        ):
            raise _fail("GetSecurityDescriptorSacl")
        status = _adv.SetNamedSecurityInfoW(
            ctypes.create_unicode_buffer(path),
            SE_FILE_OBJECT,
            LABEL_SECURITY_INFORMATION,
            None,
            None,
            None,
            sacl,
        )
        if status != 0:
            ctypes.set_last_error(status)  # type: ignore[attr-defined,unused-ignore]
            raise _fail("SetNamedSecurityInfoW(label)")
    finally:
        _k32.LocalFree(sd)


class Job:
    """A job object with memory, process-count and UI limits; closing it kills the tree."""

    def __init__(self, *, memory_bytes: int, max_processes: int) -> None:
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            raise _fail("CreateJobObjectW")
        self.handle: int = int(handle)
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | JOB_OBJECT_LIMIT_JOB_MEMORY
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        info.BasicLimitInformation.ActiveProcessLimit = max_processes
        info.JobMemoryLimit = memory_bytes
        if not _k32.SetInformationJobObject(
            self.handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self.close()
            raise _fail("SetInformationJobObject(limits)")
        ui = _UiRestrictions(JOB_UI_RESTRICTIONS)
        if not _k32.SetInformationJobObject(
            self.handle, JOB_OBJECT_BASIC_UI_RESTRICTIONS, ctypes.byref(ui), ctypes.sizeof(ui)
        ):
            self.close()
            raise _fail("SetInformationJobObject(ui)")

    def assign(self, process: int) -> None:
        if not _k32.AssignProcessToJobObject(self.handle, process):
            raise _fail("AssignProcessToJobObject")

    def terminate(self) -> None:
        if self.handle:
            _k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = 0


@dataclass(slots=True)
class Spawned:
    """A running sandboxed process: its handle, its job, and the parent ends of its pipes."""

    pid: int
    process: int
    job: Job
    stdin_fd: int | None
    stdout_fd: int
    stderr_fd: int

    def wait(self, timeout_ms: int) -> int | None:
        """Exit code, or None while it is still running after `timeout_ms`."""
        state = _k32.WaitForSingleObject(self.process, timeout_ms)
        if state == WAIT_TIMEOUT:
            return None
        code = DWORD()
        _k32.GetExitCodeProcess(self.process, ctypes.byref(code))
        return int(code.value)

    def kill(self) -> None:
        self.job.terminate()

    def close(self) -> None:
        """Kill whatever is left of the tree and release the handles."""
        self.job.terminate()
        self.job.close()
        if self.process:
            _k32.CloseHandle(self.process)
            self.process = 0


#: SDDL granting full access to the owner, the local system and ALL APPLICATION PACKAGES (AC).
#: An AppContainer child is a lower-trust token; without AC on the pipe's DACL it is denied the
#: inherited handle and cannot write its stdout - so the run looks like it produced nothing.
_PIPE_AC_SDDL = "D:(A;;GA;;;OW)(A;;GA;;;SY)(A;;GA;;;AC)"


def _security_attributes(*, inherit: bool, app_container: bool) -> _SecurityAttributes:
    attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, inherit)
    if app_container:
        sd = ctypes.c_void_p()
        if not _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            _PIPE_AC_SDDL, SDDL_REVISION_1, ctypes.byref(sd), None
        ):
            raise _fail("ConvertStringSecurityDescriptorToSecurityDescriptorW(pipe)")
        attrs.lpSecurityDescriptor = sd
    return attrs


def _pipe(*, child_reads: bool, app_container: bool = False) -> tuple[int, int]:
    """(parent_end, child_end); only the child end is inheritable."""
    attrs = _security_attributes(inherit=True, app_container=app_container)
    read, write = HANDLE(), HANDLE()
    try:
        if not _k32.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(attrs), 0):
            raise _fail("CreatePipe")
    finally:
        if attrs.lpSecurityDescriptor:
            _k32.LocalFree(attrs.lpSecurityDescriptor)
    parent, child = (write, read) if child_reads else (read, write)
    _k32.SetHandleInformation(parent, HANDLE_FLAG_INHERIT, 0)
    return int(parent.value or 0), int(child.value or 0)


def _environment_block(env: dict[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    # Windows requires the block sorted case-insensitively by name; an unsorted block makes
    # CreateProcess fail with ERROR_ENVVAR_NOT_FOUND (203), and AppContainer is stricter than
    # CreateProcessAsUser about it.
    entries = [f"{key}={value}" for key, value in sorted(env.items(), key=lambda kv: kv[0].upper())]
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0\0")


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", DWORD),
        ("Reserved", DWORD),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_userenv = ctypes.WinDLL("userenv")  # type: ignore[attr-defined,unused-ignore]
_userenv.CreateAppContainerProfile.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    DWORD,
    ctypes.POINTER(ctypes.c_void_p),
]
_userenv.CreateAppContainerProfile.restype = ctypes.c_long
_userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
    wintypes.LPCWSTR,
    ctypes.POINTER(ctypes.c_void_p),
]
_userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
_adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
_adv.ConvertSidToStringSidW.restype = BOOL
_k32.InitializeProcThreadAttributeList.argtypes = [
    ctypes.c_void_p,
    DWORD,
    DWORD,
    ctypes.POINTER(ctypes.c_size_t),
]
_k32.InitializeProcThreadAttributeList.restype = BOOL
_k32.UpdateProcThreadAttribute.argtypes = [
    ctypes.c_void_p,
    DWORD,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
_k32.UpdateProcThreadAttribute.restype = BOOL
_k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
_k32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    BOOL,
    DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
    ctypes.POINTER(_ProcessInformation),
]
_k32.CreateProcessW.restype = BOOL
_k32.TerminateProcess.argtypes = [HANDLE, wintypes.UINT]
_k32.TerminateProcess.restype = BOOL


PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_AttrValue = ctypes.Structure | ctypes.Array[HANDLE]


class _AttributeList:
    """A `PROC_THREAD_ATTRIBUTE_LIST` and the buffers its entries point at (kept alive with it).

    Every sandboxed child is created with `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` naming exactly its own
    three standard handles. `bInheritHandles` must be TRUE for stdio redirection, and without the
    list *every* inheritable handle the core holds at that moment - another run's pipe, a socket,
    a file - would leak into the child (review finding 8).
    """

    def __init__(self, count: int) -> None:
        size = ctypes.c_size_t()
        _k32.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
        self.buffer = ctypes.create_string_buffer(size.value)
        if not _k32.InitializeProcThreadAttributeList(self.buffer, count, 0, ctypes.byref(size)):
            raise _fail("InitializeProcThreadAttributeList")
        self._keep: list[object] = []
        self._initialised = True

    def set(self, attribute: int, value: _AttrValue, size: int) -> None:
        self._keep.append(value)
        if not _k32.UpdateProcThreadAttribute(
            self.buffer, 0, attribute, ctypes.byref(value), size, None, None
        ):
            raise _fail(f"UpdateProcThreadAttribute({attribute:#x})")

    def handle_list(self, handles: list[int]) -> None:
        array = (HANDLE * len(handles))(*handles)
        self.set(PROC_THREAD_ATTRIBUTE_HANDLE_LIST, array, ctypes.sizeof(array))

    @property
    def pointer(self) -> int:
        return ctypes.addressof(self.buffer)

    def close(self) -> None:
        if self._initialised:
            _k32.DeleteProcThreadAttributeList(self.buffer)
            self._initialised = False


def app_container_sid(name: str) -> str:
    """The SID string of the AppContainer profile `name`, creating the profile on first use.

    Creating a profile needs no administrator rights. The SID is derived from the name, so it is
    stable across restarts - which is what lets a folder ACL granted once keep working.
    """
    sid = ctypes.c_void_p()
    status = _userenv.CreateAppContainerProfile(name, name, name, None, 0, ctypes.byref(sid))
    if status != 0:  # already exists (HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)) or other
        status = _userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))
        if status != 0:
            raise SandboxWin32Error(status, f"AppContainer profile {name!r} unavailable")
    text = wintypes.LPWSTR()
    try:
        if not _adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise _fail("ConvertSidToStringSidW")
        return str(text.value)
    finally:
        _k32.LocalFree(text)


def spawn(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    memory_bytes: int,
    max_processes: int,
    with_stdin: bool = False,
    app_container: str | None = None,
) -> Spawned:
    """Start `argv` sandboxed inside a fresh limited job; returns before it exits.

    `app_container=None` runs it with a low-integrity copy of the core's token. With a SID string
    it runs inside that AppContainer instead (low integrity too, plus: no network capability, no
    access to the user's files or credentials, only what is ACL'd for the container SID).

    The process is created suspended, put in the job, and only then resumed, so not one
    instruction of it runs outside the job.
    """
    if app_container is not None:
        return _spawn_app_container(
            argv,
            cwd=cwd,
            env=env,
            memory_bytes=memory_bytes,
            max_processes=max_processes,
            with_stdin=with_stdin,
            sid_string=app_container,
        )
    token = low_integrity_token()
    job = Job(memory_bytes=memory_bytes, max_processes=max_processes)
    to_close: list[int] = []
    attributes: _AttributeList | None = None
    try:
        in_parent, out_parent, err_parent, in_child, out_child, err_child, to_close = _std_handles(
            with_stdin
        )
        attributes = _AttributeList(1)
        attributes.handle_list([in_child, out_child, err_child])
        startup = _startup_info(in_child, out_child, err_child, attributes)
        info = _ProcessInformation()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        block = _environment_block(env)
        flags = (
            CREATE_SUSPENDED
            | CREATE_UNICODE_ENVIRONMENT
            | CREATE_NO_WINDOW
            | EXTENDED_STARTUPINFO_PRESENT
        )
        ok = _adv.CreateProcessAsUserW(
            token,
            None,
            command,
            None,
            None,
            True,
            flags | CREATE_BREAKAWAY_FROM_JOB,
            block,
            cwd,
            ctypes.byref(startup),
            ctypes.byref(info),
        )
        if not ok:
            # Breakaway is refused when the core's own job does not allow it; the sandbox job
            # then nests inside the core's job, which Windows 8+ supports.
            ok = _adv.CreateProcessAsUserW(
                token,
                None,
                command,
                None,
                None,
                True,
                flags,
                block,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(info),
            )
        if not ok:
            raise _fail("CreateProcessAsUserW")
        return _start_in_job(info, job, in_parent, out_parent, err_parent)
    except BaseException:
        job.close()
        raise
    finally:
        if attributes is not None:
            attributes.close()
        for handle in to_close:
            _k32.CloseHandle(handle)
        _k32.CloseHandle(token)


def _startup_info(
    in_child: int, out_child: int, err_child: int, attributes: _AttributeList
) -> _StartupInfoEx:
    startup = _StartupInfoEx()
    startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
    startup.StartupInfo.wShowWindow = SW_HIDE
    startup.StartupInfo.hStdInput = in_child
    startup.StartupInfo.hStdOutput = out_child
    startup.StartupInfo.hStdError = err_child
    startup.lpAttributeList = attributes.pointer
    return startup


def _start_in_job(
    info: _ProcessInformation,
    job: Job,
    in_parent: int | None,
    out_parent: int,
    err_parent: int,
) -> Spawned:
    """Put the suspended process in its job, then let it run; returns the parent's view of it."""
    import msvcrt

    try:
        job.assign(info.hProcess)
    except SandboxWin32Error:
        _k32.TerminateProcess(info.hProcess, 1)
        _k32.CloseHandle(info.hThread)
        _k32.CloseHandle(info.hProcess)
        raise
    _k32.ResumeThread(info.hThread)
    _k32.CloseHandle(info.hThread)
    return Spawned(
        pid=int(info.dwProcessId),
        process=int(info.hProcess),
        job=job,
        stdin_fd=msvcrt.open_osfhandle(in_parent, 0) if in_parent is not None else None,  # type: ignore[attr-defined,unused-ignore]
        stdout_fd=msvcrt.open_osfhandle(out_parent, os.O_RDONLY),  # type: ignore[attr-defined,unused-ignore]
        stderr_fd=msvcrt.open_osfhandle(err_parent, os.O_RDONLY),  # type: ignore[attr-defined,unused-ignore]
    )


def _std_handles(
    with_stdin: bool, *, app_container: bool = False
) -> tuple[int | None, int, int, int, int, int, list[int]]:
    """Parent ends (stdin or None, stdout, stderr), child ends (in, out, err), handles to close."""
    out_parent, out_child = _pipe(child_reads=False, app_container=app_container)
    err_parent, err_child = _pipe(child_reads=False, app_container=app_container)
    in_parent: int | None = None
    if with_stdin:
        in_parent, in_child = _pipe(child_reads=True, app_container=app_container)
    else:
        attrs = _security_attributes(inherit=True, app_container=app_container)
        try:
            in_child = int(_k32.CreateFileW("NUL", 0x80000000, 3, ctypes.byref(attrs), 3, 0, None))
        finally:
            if attrs.lpSecurityDescriptor:
                _k32.LocalFree(attrs.lpSecurityDescriptor)
    return (
        in_parent,
        out_parent,
        err_parent,
        in_child,
        out_child,
        err_child,
        [
            in_child,
            out_child,
            err_child,
        ],
    )


def _spawn_app_container(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    memory_bytes: int,
    max_processes: int,
    with_stdin: bool,
    sid_string: str,
) -> Spawned:
    sid = ctypes.c_void_p()
    if not _adv.ConvertStringSidToSidW(sid_string, ctypes.byref(sid)):
        raise _fail("ConvertStringSidToSidW(AppContainer)")
    job = Job(memory_bytes=memory_bytes, max_processes=max_processes)
    to_close: list[int] = []
    attributes: _AttributeList | None = None
    try:
        in_parent, out_parent, err_parent, in_child, out_child, err_child, to_close = _std_handles(
            with_stdin, app_container=True
        )
        attributes = _AttributeList(2)
        capabilities = _SecurityCapabilities(sid, None, 0, 0)
        attributes.set(
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, capabilities, ctypes.sizeof(capabilities)
        )
        attributes.handle_list([in_child, out_child, err_child])
        startup = _startup_info(in_child, out_child, err_child, attributes)
        info = _ProcessInformation()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        block = _environment_block(env)
        flags = (
            CREATE_SUSPENDED
            | CREATE_UNICODE_ENVIRONMENT
            | CREATE_NO_WINDOW
            | EXTENDED_STARTUPINFO_PRESENT
        )
        if not _k32.CreateProcessW(
            None,
            command,
            None,
            None,
            True,
            flags,
            block,
            cwd,
            ctypes.byref(startup),
            ctypes.byref(info),
        ):
            raise _fail("CreateProcessW(AppContainer)")
        return _start_in_job(info, job, in_parent, out_parent, err_parent)
    except BaseException:
        job.close()
        raise
    finally:
        if attributes is not None:
            attributes.close()
        for handle in to_close:
            _k32.CloseHandle(handle)
        _k32.LocalFree(sid)
