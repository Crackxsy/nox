"""`build_bwrap_argv`: the Linux confinement is in the argv, so assert it (runs on any OS)."""

from __future__ import annotations

from nox.sandbox.linux import build_bwrap_argv


def test_no_network_uses_unshare_net() -> None:
    argv = build_bwrap_argv(["echo", "hi"], work="/w", env={}, share_loopback=False)
    assert "--unshare-net" in argv
    assert argv[:1] == ["bwrap"]
    assert argv[-2:] == ["echo", "hi"]


def test_system_is_read_only_and_work_is_writable() -> None:
    argv = build_bwrap_argv(["x"], work="/w", env={}, share_loopback=False)
    joined = " ".join(argv)
    # System dirs are bound read-only (try, so a missing one is skipped); the work dir is rw.
    assert "--ro-bind-try" in argv
    assert "--bind /w /w" in joined
    assert "--chdir /w" in joined
    assert "--clearenv" in argv


def test_env_is_set_explicitly() -> None:
    argv = build_bwrap_argv(
        ["x"], work="/w", env={"HTTPS_PROXY": "http://127.0.0.1:9"}, share_loopback=True
    )
    assert "--setenv" in argv
    i = argv.index("--setenv")
    assert argv[i + 1] == "HTTPS_PROXY" and argv[i + 2] == "http://127.0.0.1:9"


def test_declared_egress_still_unshares_net() -> None:
    # Even with egress, the namespace is private (only loopback + the host nftables rule reach out).
    argv = build_bwrap_argv(["x"], work="/w", env={}, share_loopback=True)
    assert "--unshare-net" in argv
