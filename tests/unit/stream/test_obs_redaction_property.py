"""Property: whatever mix of paths, URLs, tokens and text OBS answers with, the redacted string
never shows a directory in front of a file name, never a URL's user info, path, query or
fragment, and never a generated-looking run of 20 or more key-like characters."""

from __future__ import annotations

import re
import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nox.stream.obs_requests import high_entropy, redact

ALNUM = string.ascii_letters + string.digits

#: Directory and URL parts carry markers no other generated part contains, so their absence in
#: the output can be checked by substring.
dir_segment = st.text(alphabet="abcdefgh xyz", min_size=1, max_size=8).map(lambda s: f"DIRSEG{s}")
file_name = st.text(alphabet="abcdefgh", min_size=1, max_size=8).map(lambda s: f"{s}.png")
path_root = st.sampled_from(["C:\\", "c:/", "D:/", "\\\\server\\share\\", "/", "~/", "/home/"])


@st.composite
def absolute_path(draw: st.DrawFn) -> str:
    root = draw(path_root)
    dirs = draw(st.lists(dir_segment, min_size=1, max_size=4))
    sep = "\\" if root.endswith("\\") else "/"
    return root + sep.join(dirs) + sep + draw(file_name)


@st.composite
def url(draw: st.DrawFn) -> str:
    scheme = draw(st.sampled_from(["http", "https", "rtmp", "rtmps", "ftp", "obs-x", "srt"]))
    user = draw(
        st.sampled_from(
            ["", "USERINFO:PWSECRET@", "USERINFO@", "USERINFO:@", ":PWSECRET@", "a@USERINFO@"]
        )
    )
    host = draw(st.sampled_from(["127.0.0.1:47801", "live.example.tv", "localhost"]))
    path = "/".join(draw(st.lists(st.just("URLPATH"), min_size=0, max_size=3)))
    query = draw(st.sampled_from(["", "?QUERYKEY=QUERYVAL"]))
    fragment = draw(st.sampled_from(["", "#FRAGMENT", "#x@USERINFO"]))
    after_path = draw(st.sampled_from(["", "@USERINFO", "/URLPATH@USERINFO:PWSECRET"]))
    return f"{scheme}://{user}{host}/{path}{after_path}{query}{fragment}"


token = (
    st.text(alphabet=ALNUM + "+/=_-", min_size=20, max_size=48)
    .filter(high_entropy)
    .map(lambda t: f"TOK{t}")
)
words = st.text(alphabet="abcdefg hijk", min_size=1, max_size=20)
#: Whatever may stand right in front of a path or URL: letters, digits, punctuation, a colon.
glued = st.text(alphabet="xyz019.:;!-_()=,'\"", max_size=3)
part = st.one_of(
    st.tuples(glued, absolute_path()).map("".join),
    st.tuples(glued, url()).map("".join),
    token,
    words,
)
separator = st.sampled_from([" ", "  ", "\n", " (", ") ", ": ", "\t"])


@st.composite
def obs_text(draw: st.DrawFn) -> tuple[str, list[str]]:
    parts = draw(st.lists(part, min_size=1, max_size=6))
    seps = draw(st.lists(separator, min_size=len(parts), max_size=len(parts)))
    tokens = [p for p in parts if p.startswith("TOK")]
    return "".join(p + s for p, s in zip(parts, seps, strict=True)), tokens


URL_LEAKS = ("URLPATH", "QUERYKEY", "QUERYVAL", "FRAGMENT", "USERINFO", "PWSECRET")
KEYLIKE = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(obs_text())
def test_redacted_text_leaks_no_directory_url_detail_or_key(sample: tuple[str, list[str]]) -> None:
    text, tokens = sample
    for out in (
        redact({"comment": text})["comment"],
        redact({"inputSettings": {"text": text}})["inputSettings"]["text"],
    ):
        assert "DIRSEG" not in out, out
        for leak in URL_LEAKS:
            assert leak not in out, out
        for tok in tokens:
            secret = tok[3:]
            # Not the secret, and no 12-character piece of it either.
            assert not any(secret[i : i + 12] in out for i in range(len(secret) - 11)), out
        assert not [run for run in KEYLIKE.findall(out) if high_entropy(run)], out


@settings(max_examples=200, deadline=None)
@given(absolute_path())
def test_a_path_alone_becomes_its_file_name(path: str) -> None:
    out = redact({"savedReplayPath": path})["savedReplayPath"]
    assert out.endswith(".png") and "/" not in out and "\\" not in out


@pytest.mark.parametrize(
    "value",
    [
        "x1https://host.example/TOKENPATH",  # a scheme character right in front
        ".https://host.example/TOKENPATH",
        "1://host.example/TOKENPATH",  # not a scheme at all, still a URL-like tail
        "key:/home/DIRSEGuser/notes.txt",  # a path right after a colon
        "x/home/DIRSEGuser/notes.txt",
    ],
)
def test_review_edge_cases(value: str) -> None:
    out = redact({"comment": value})["comment"]
    assert "TOKENPATH" not in out and "DIRSEG" not in out, out


@settings(max_examples=200, deadline=None)
@given(url(), st.sampled_from(["url", "input", "server"]))
def test_url_settings_are_a_host_or_redacted(value: str, key: str) -> None:
    for candidate in (value, value.split("://", 1)[1]):  # with and without a scheme
        out = redact({"inputSettings": {key: candidate}})["inputSettings"][key]
        for leak in URL_LEAKS:
            assert leak not in out, out
        assert out == "[redacted]" or "/" not in out.split("://", 1)[-1], out


@settings(max_examples=100, deadline=None)
@given(token, st.integers(min_value=-30, max_value=30))
def test_truncation_never_leaves_half_a_key(tok: str, shift: int) -> None:
    from nox.stream.obs_requests import MAX_STRING_CHARS

    filler = "x " * ((MAX_STRING_CHARS + shift - 10) // 2)
    out = redact({"comment": filler + tok + " tail"})["comment"]
    secret = tok[3:]
    assert not any(secret[i : i + 8] in out for i in range(len(secret) - 7)), out[-80:]


@pytest.mark.parametrize(
    "value",
    [
        "https://user:pass@host.example/x",
        "https://host.example/x@user:pass",
        "https://host.example@evil.example/x",
        "https://user%40x:pw@host.example",
        "https://[::1]:8080/x",
    ],
)
def test_userinfo_variants(value: str) -> None:
    out = redact({"comment": value})["comment"]
    assert "pass" not in out and "user" not in out and "/x" not in out, out
