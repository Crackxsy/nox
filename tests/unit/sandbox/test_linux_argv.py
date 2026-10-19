"""`build_bwrap_argv`: the Linux confinement is in the argv, so assert it (runs on any OS)."""

from __future__ import annotations

from nox.sandbox.linux import build_bwrap_argv


def test_no_network_uses_unshare_net() -> None:
    argv = build_bwrap_argv(["echo", "hi"], work="/w", share_loopback=False)
    assert "--unshare-net" in argv
    assert argv[:1] == ["bwrap"]
    assert argv[-2:] == ["echo", "hi"]


def test_system_is_read_only_and_work_is_writable() -> None:
    argv = build_bwrap_argv(["x"], work="/w", share_loopback=False)
    joined = " ".join(argv)
    # System dirs are bound read-only (try, so a missing one is skipped); the work dir is rw.
    assert "--ro-bind-try" in argv
    assert "--bind /w /w" in joined
    assert "--chdir /w" in joined
    # No --clearenv/--setenv: the environment is the process environment, never argv.


def test_the_environment_never_reaches_the_command_line() -> None:
    """The proxy secret (and any server setting) must not be world-readable in /proc/cmdline."""
    from nox.sandbox.linux import LinuxSandbox
    from nox.sandbox.model import EgressDecl, Limits

    sandbox = LinuxSandbox.__new__(LinuxSandbox)  # no bwrap needed to build the argv
    sandbox._has_systemd_run = True  # noqa: SLF001
    secret = "s3cr3t-proxy-token"
    egress = EgressDecl(hosts=("example.com:443",))
    argv = sandbox.command(["srv"], work="/w", egress=egress, limits=Limits())
    env = sandbox.environment("/w", 4711, {"MCP_TOKEN": "tok"}, f"nox:{secret}")
    assert not any(secret in part or "tok" == part for part in argv)
    assert "--setenv" not in argv and "--clearenv" not in argv
    assert env["HTTPS_PROXY"] == f"http://nox:{secret}@127.0.0.1:4711"
    assert env["MCP_TOKEN"] == "tok" and env["PATH"] == "/usr/bin:/bin"
    assert set(env) <= {"PATH", "HOME", "TMPDIR", "MCP_TOKEN"} | {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
    }


def test_declared_egress_still_unshares_net() -> None:
    # Even with egress, the namespace is private (only loopback + the host nftables rule reach out).
    argv = build_bwrap_argv(["x"], work="/w", share_loopback=True)
    assert "--unshare-net" in argv
