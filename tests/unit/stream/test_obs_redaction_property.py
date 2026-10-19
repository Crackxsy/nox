"""Property: whatever mix of paths, URLs, tokens and text OBS answers with, the redacted string
never shows a directory in front of a file name, never a URL's user info, path, query or
fragment, and never a generated-looking run of 20 or more key-like characters."""

from __future__ import annotations

import re

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from nox.stream.obs_requests import high_entropy, redact

ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

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
    user = draw(st.sampled_from(["", "USERINFO:PWSECRET@"]))
    host = draw(st.sampled_from(["127.0.0.1:47801", "live.example.tv", "localhost"]))
    path = "/".join(draw(st.lists(st.just("URLPATH"), min_size=0, max_size=3)))
    query = draw(st.sampled_from(["", "?QUERYKEY=QUERYVAL"]))
    fragment = draw(st.sampled_from(["", "#FRAGMENT"]))
    return f"{scheme}://{user}{host}/{path}{query}{fragment}"


token = (
    st.text(alphabet=ALNUM + "+/=_-", min_size=20, max_size=48)
    .filter(high_entropy)
    .map(lambda t: f"TOK{t}")
)
words = st.text(alphabet="abcdefg hijk", min_size=1, max_size=20)
part = st.one_of(absolute_path(), url(), token, words)
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
    for key in ("comment", "inputSettings"):
        value = {"text": text} if key == "inputSettings" else text
        out = str(redact({key: value}))
        assert "DIRSEG" not in out, out
        for leak in URL_LEAKS:
            assert leak not in out, out
        for tok in tokens:
            assert tok[3:] not in out and tok[3:23] not in out, out
        assert not [run for run in KEYLIKE.findall(out) if high_entropy(run)], out


@settings(max_examples=200, deadline=None)
@given(absolute_path())
def test_a_path_alone_becomes_its_file_name(path: str) -> None:
    out = redact({"savedReplayPath": path})["savedReplayPath"]
    assert out.endswith(".png") and "/" not in out and "\\" not in out
