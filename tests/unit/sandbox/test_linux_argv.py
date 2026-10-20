"""The Linux backend's command lines and launcher, asserted without bwrap (runs on any OS).

The real thing - namespaces, the egress path, the ceilings - is proven on Linux CI by
`tests/integration/test_linux_sandbox.py`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nox.sandbox import Sandbox
from nox.sandbox import linux as linux_mod
from nox.sandbox._cgroup import DelegatedCgroups
from nox.sandbox.linux import (
    EGRESS_PORT,
    Ceiling,
    LinuxSandbox,
    build_bwrap_argv,
    forwarder_python,
    safe_python_binds,
)
from nox.sandbox.model import EgressDecl, Guarantees, Limits, SandboxUnavailable
from nox.sandbox.netguard import NetGuard

EGRESS = EgressDecl(hosts=("example.com:443",))


def _backend(monkeypatch: pytest.MonkeyPatch, *, python: bool = True) -> LinuxSandbox:
    monkeypatch.setattr(linux_mod, "bwrap_path", lambda: "bwrap")
    sandbox = LinuxSandbox(probe=False)  # no bwrap on this machine needed to build the argv
    sandbox._python = ("/usr/bin/python3", ()) if python else None  # noqa: SLF001
    return sandbox


def _after_separator(argv: list[str]) -> list[str]:
    return argv[argv.index("--") + 1 :]


def test_no_egress_has_no_network_and_no_forwarder() -> None:
    argv = build_bwrap_argv(["echo", "hi"], work="/w")
    assert argv[:1] == ["bwrap"]
    assert "--unshare-net" in argv
    assert "--share-net" not in argv
    assert _after_separator(argv) == ["echo", "hi"]
    assert not any("egress" in part for part in argv)


def test_system_is_read_only_and_work_is_writable() -> None:
    argv = build_bwrap_argv(["x"], work="/w")
    joined = " ".join(argv)
    assert "--ro-bind-try /usr /usr" in joined
    assert "--bind /w /w" in joined
    assert "--chdir /w" in joined
    assert "--dev /dev" in joined and "--proc /proc" in joined
    assert "--setenv" not in argv and "--clearenv" not in argv


def test_declared_egress_keeps_the_network_unshared_and_adds_only_the_socket() -> None:
    argv = build_bwrap_argv(["srv", "--flag"], work="/w", egress_socket="/tmp/nox-egress-x/s")
    assert "--unshare-net" in argv, "egress never opens a network interface"
    joined = " ".join(argv)
    # The proxy socket is bound read-only into a fixed place; nothing else from the host is added.
    assert "--ro-bind /tmp/nox-egress-x/s /run/nox-sandbox/egress.sock" in joined
    assert "/run/nox-sandbox/egress_forwarder.py" in joined
    # The target runs behind the forwarder, which gets only the socket path and the port.
    assert _after_separator(argv) == [
        "/usr/bin/python3",
        "-I",
        "-S",
        "/run/nox-sandbox/egress_forwarder.py",
        "/run/nox-sandbox/egress.sock",
        str(EGRESS_PORT),
        "--",
        "srv",
        "--flag",
    ]


def test_forwarder_source_ships_with_the_package() -> None:
    assert Path(linux_mod._FORWARDER_SRC).is_file()  # noqa: SLF001


def test_a_python_outside_usr_is_bound_read_only() -> None:
    argv = build_bwrap_argv(
        ["x"],
        work="/w",
        egress_socket="/s",
        python="/opt/py/bin/python3",
        python_binds=("/opt/py",),
    )
    assert "--ro-bind /opt/py /opt/py" in " ".join(argv)
    assert _after_separator(argv)[0] == "/opt/py/bin/python3"


def test_forwarder_python_prefers_the_system_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(linux_mod.os, "access", lambda path, mode: path == "/usr/bin/python3")
    assert forwarder_python() == ("/usr/bin/python3", ())


def test_forwarder_python_falls_back_to_minimal_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    exe = "/home/x/.local/share/uv/python/cpython-3.13/bin/python3.13"
    monkeypatch.setattr(linux_mod.os, "access", lambda path, mode: path == exe)
    monkeypatch.setattr(linux_mod.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(linux_mod.sys, "executable", exe)
    monkeypatch.setattr(linux_mod, "_own_python_binds", lambda real: (real, "/lib-of-it"))
    assert forwarder_python() == (exe, (exe, "/lib-of-it"))
    monkeypatch.setattr(linux_mod, "_own_python_binds", lambda real: None)
    assert forwarder_python() is None, "a too-wide bind refuses the egress path"


UV = "/home/x/.local/share/uv/python/cpython-3.13-linux"


def test_python_binds_are_only_the_interpreter_pieces() -> None:
    binds = safe_python_binds(
        [f"{UV}/bin/python3.13", f"{UV}/lib/python3.13", f"{UV}/lib/libpython3.13.so.1.0"],
        private=["/home/x", "/home/x/.local/share/nox"],
    )
    assert binds == (
        f"{UV}/bin/python3.13",
        f"{UV}/lib/python3.13",
        f"{UV}/lib/libpython3.13.so.1.0",
    )
    assert binds is not None and "/home/x" not in binds


@pytest.mark.parametrize(
    "wide",
    [
        "/",
        "/home",
        "/home/x",
        "/home/x/",
        "/root",
        "/var",
        "/var/lib",  # contains Nox's own folder below
        "/home/x/.local",  # contains private data of the user's Nox
    ],
)
def test_too_wide_python_binds_fail_closed(wide: str) -> None:
    private = ["/home/x", "/var/lib/nox", "/home/x/.local/share/nox"]
    assert safe_python_binds([f"{UV}/bin/python3.13", wide], private=private) is None


def test_the_environment_never_reaches_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy secret (and any server setting) must not be world-readable in /proc/cmdline."""
    sandbox = _backend(monkeypatch)
    sandbox.ceiling = Ceiling.SCOPE
    secret = "s3cr3t-proxy-token"
    argv = sandbox.command(
        ["srv"], work="/w", egress=EGRESS, limits=Limits(), proxy_socket="/tmp/e/s"
    )
    env = sandbox.environment("/w", EGRESS_PORT, {"MCP_TOKEN": "tok"}, f"nox:{secret}")
    assert not any(secret in part or part == "tok" for part in argv)
    assert argv[:2] == ["systemd-run", "--scope"]
    url = f"http://nox:{secret}@127.0.0.1:{EGRESS_PORT}"
    assert env["HTTPS_PROXY"] == env["http_proxy"] == env["ALL_PROXY"] == url
    assert env["MCP_TOKEN"] == "tok" and env["PATH"] == "/usr/bin:/bin"


def test_no_egress_means_no_proxy_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _backend(monkeypatch).environment("/w", None, {}, "")
    assert not any("proxy" in key.lower() for key in env)


def test_declared_egress_without_a_socket_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = _backend(monkeypatch)
    with pytest.raises(SandboxUnavailable):
        sandbox.command(["x"], work="/w", egress=EGRESS, limits=Limits())


def test_declared_egress_without_a_forwarder_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _backend(monkeypatch, python=False)
    with pytest.raises(SandboxUnavailable):
        sandbox.command(["x"], work="/w", egress=EGRESS, limits=Limits(), proxy_socket="/s")
    assert any("egress" in note for note in sandbox.guarantees().notes)
    # ...while a run without egress still works.
    assert sandbox.command(["x"], work="/w", egress=EgressDecl(), limits=Limits())


def test_user_scope_keeps_the_bus_out_of_the_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = _backend(monkeypatch)
    sandbox.ceiling = Ceiling.USER_SCOPE
    argv = sandbox.command(["x"], work="/w", egress=EgressDecl(), limits=Limits(memory_bytes=5))
    assert argv[:3] == ["systemd-run", "--user", "--scope"]
    assert "-pMemoryMax=5" in argv
    bwrap = argv[argv.index("bwrap") :]
    assert "--unsetenv XDG_RUNTIME_DIR" in " ".join(bwrap)


def test_cgroup_ceiling_wraps_bwrap_in_the_trampoline(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = _backend(monkeypatch)
    sandbox.ceiling = Ceiling.CGROUP
    cg = Path("/sys/fs/cgroup/system.slice/nox.service/sbx-1")
    argv = sandbox.command(["x"], work="/w", egress=EgressDecl(), limits=Limits(), cgroup=cg)
    assert argv[:2] == ["/bin/sh", "-c"]
    assert "cgroup.procs" in argv[2] and 'exec "$@"' in argv[2]
    assert argv[3] == str(cg) and argv[4] == "bwrap"


def test_no_ceiling_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = _backend(monkeypatch)
    assert sandbox.ceiling is Ceiling.NONE
    assert any("ceiling" in note for note in sandbox.guarantees().notes)
    assert sandbox.guarantees().strong


def test_cgroup_create_writes_the_ceilings(tmp_path: Path) -> None:
    cgroups = DelegatedCgroups(tmp_path)
    path = cgroups.create(Limits(memory_bytes=1234, max_processes=7))
    assert (path / "memory.max").read_text() == "1234"
    assert (path / "pids.max").read_text() == "7"


def test_cgroups_are_used_only_when_the_unit_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOX_CGROUP_DELEGATED", raising=False)
    assert DelegatedCgroups.setup() is None


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets: the Linux transport")
async def test_sandbox_gives_a_unix_backend_a_socket_not_a_port() -> None:
    seen: dict[str, object] = {}

    class Backend:
        mode = linux_mod.SandboxMode.LINUX_BWRAP
        proxy_transport = "unix"

        def guarantees(self) -> Guarantees:
            # Sandbox.run refuses a backend weaker than accepted, so the fake states its own.
            return Guarantees(
                backend="fake",
                confines_writes=True,
                confines_reads=True,
                hides_secrets=True,
                confines_network=True,
            )

        async def run(self, argv, **kw):  # noqa: ANN001, ANN003, ANN202
            seen.update(kw)
            seen["exists"] = os.path.exists(kw["proxy_socket"])  # noqa: ASYNC240
            return "ran"

    sandbox = Sandbox(Backend(), guard=NetGuard())  # type: ignore[arg-type]
    assert await sandbox.run(["x"], egress=EGRESS) == "ran"
    assert seen["exists"] is True and seen["proxy_port"] is None
    assert str(seen["proxy_auth"]).startswith("nox:")
    gone = not os.path.exists(str(seen["proxy_socket"]))  # noqa: ASYNC240
    assert gone, "the socket goes with the run"


def test_no_nested_user_namespaces_inside() -> None:
    argv = build_bwrap_argv(["x"], work="/w", bwrap="/usr/lib/nox/bwrap")
    assert argv[:3] == ["/usr/lib/nox/bwrap", "--unshare-user", "--disable-userns"]
    old = build_bwrap_argv(["x"], work="/w", disable_userns=False)
    assert "--disable-userns" not in old


def test_an_old_bwrap_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = _backend(monkeypatch)
    sandbox.disables_userns = False
    assert any("nested user namespaces" in n for n in sandbox.guarantees().notes)
    argv = sandbox.command(["x"], work="/w", egress=EgressDecl(), limits=Limits())
    assert "--disable-userns" not in argv


def test_nox_bwrap_copy_must_match_the_system_bwrap(tmp_path: Path) -> None:
    system, copy = tmp_path / "bwrap", tmp_path / "nox-bwrap"
    system.write_bytes(b"bwrap 0.9.0")
    copy.write_bytes(b"bwrap 0.9.0")
    copy.chmod(0o750)
    if not os.access(copy, os.X_OK):
        pytest.skip("no executable bit on this filesystem")
    assert linux_mod.bwrap_path(str(copy), str(system)) == str(copy)
    system.write_bytes(b"bwrap 0.9.1 (security update)")
    with pytest.raises(SandboxUnavailable, match="installer again"):
        linux_mod.bwrap_path(str(copy), str(system))


@pytest.mark.parametrize(
    "label",
    [
        "nox-bwrap (enforce)",  # `pix` fell back: the permissive parent profile only
        "unconfined",
        "nox-bwrap-sandboxed-not (enforce)",
        "",
        None,
    ],
)
def test_nox_bwrap_is_refused_unless_the_stacked_profile_applies(label: str | None) -> None:
    from nox.sandbox.linux import check_apparmor_stack

    with pytest.raises(SandboxUnavailable, match="nox-bwrap-sandboxed"):
        check_apparmor_stack(label)


@pytest.mark.parametrize(
    "label", ["nox-bwrap//&nox-bwrap-sandboxed (enforce)", "nox-bwrap-sandboxed (enforce)"]
)
def test_the_stacked_profile_passes(label: str) -> None:
    from nox.sandbox.linux import check_apparmor_stack

    check_apparmor_stack(label)


def test_the_launchers_get_a_fixed_environment_and_the_child_its_own_through_a_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Re-review HIGH: the child's environment (a server's settings, the proxy secret) was the
    # environment of the host-side launchers too, so LD_AUDIT, GCONV_PATH, SYSTEMD_* ... in it
    # acted outside the sandbox. Now bwrap starts with --clearenv and reads the child's
    # variables from a pipe; systemd-run and bwrap run with LAUNCHER_ENV only.
    import os

    from nox.sandbox.linux import LAUNCHER_ENV

    sandbox = _backend(monkeypatch)
    sandbox.ceiling = Ceiling.SCOPE
    secret = "s3cr3t-proxy-token"
    launch = sandbox._prepare(  # noqa: SLF001
        ["srv"],
        cwd=str(tmp_path),
        egress=EGRESS,
        limits=Limits(),
        extra_env={"MCP_TOKEN": "tok"},
        proxy_auth=f"nox:{secret}",
        proxy_socket="/tmp/e/s",
    )
    try:
        assert launch.env == dict(LAUNCHER_ENV)
        assert launch.env_fd is not None
        bwrap_at = launch.cmd.index("bwrap")
        assert launch.cmd[bwrap_at + 1 : bwrap_at + 4] == [
            "--clearenv",
            "--args",
            str(launch.env_fd),
        ]
        assert not any(secret in part or part == "tok" for part in launch.cmd)
        data = os.read(launch.env_fd, 65536).split(b"\0")
        assert data[:3] == [b"--setenv", b"MCP_TOKEN", b"tok"]
        assert f"http://nox:{secret}@127.0.0.1:{EGRESS_PORT}".encode() in data
    finally:
        launch.close_env()


def test_env_args_refuses_what_bwrap_could_misread() -> None:
    from nox.sandbox.linux import env_args

    assert env_args({"A": "1"}) == b"--setenv\0A\0" + b"1\0"
    for bad in ({"A\0B": "1"}, {"A": "x\0y"}, {"A=B": "1"}, {"": "1"}):
        with pytest.raises(SandboxUnavailable):
            env_args(bad)
