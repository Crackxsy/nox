"""`filter_outbound`: what a line has to look like before Nox may send it - no links, no chat
commands, no mass mentions, no invisible characters, one short line. Every rule is a literal table
row, including the usual ways around it (full-width forms, look-alikes, padding, punycode)."""

from __future__ import annotations

import pytest
from nox_plugin_twitch.outbound import (
    BLOCKED_COMMAND_PREFIX,
    BLOCKED_EMPTY,
    BLOCKED_LINK,
    MAX_MENTIONS,
    MAX_OUTBOUND_CHARS,
    filter_outbound,
)

ZWSP = "​"
ZWJ = "‍"
BOM = "﻿"

# -- lines that are fine as they are ------------------------------------------------------------

HARMLESS = [
    "Klar, mach ich!",
    "gg everyone",
    "Hallo @viewer_01, willkommen!",
    "Wie geht's? Mir gut. In der Mitte des Spiels wird es spannend.",
    "Das war z.B. ein guter Zug, d.h. wir machen weiter.",
    "Version 3.5 ist da, um 19.30 Uhr geht es los.",
    "Das war toll! Danke an Alex.",
    "Ich habe stein gewählt - du gewinnst!",
    "Usage: !rps rock|paper|scissors (or: stein|schere|papier)",
    "Befehle: !rps <stein|schere|papier>, !funken, !streamplan, !befehle",
    "Mo, Mi, Fr ab 19 Uhr",
    "Warte noch 12s bis zum nächsten !rps.",
    "Hmm... wirklich?",
    "Ende. Danke",
    "Unterstützung für Ä, Ö, Ü und ß gibt es auch",
    "🎉 Glückwunsch!",
]


@pytest.mark.parametrize("text", HARMLESS)
def test_a_harmless_line_is_sent_unchanged(text: str) -> None:
    result = filter_outbound(text)
    assert result.blocked == ""
    assert result.text == " ".join(text.split())


def test_a_clean_line_reports_no_changes() -> None:
    assert filter_outbound("gg everyone").changes == ()


# -- links --------------------------------------------------------------------------------------

LINKS = [
    "https://example.invalid/x",
    "http://example.invalid",
    "HTTPS://EXAMPLE.INVALID",
    "ftp://files.example.invalid",
    "see https: example",
    "schau mal: example.invalid/hilfe",
    "www.example.invalid",
    "WWW . example.invalid",
    "example.com",
    "Example.COM/path",
    "a.b.example.org",
    "discord.gg/abc123",
    "bit.ly/x1",
    "t.co/abc",
    "twitch.tv/alex",
    "mail me at alex@example.com",
    "example.de",
    "example.io",
    # an ending nobody listed
    "example.invalid",
    "evil.lol",
    "files.zip",
    "a.b.c.example.museum",
    # white space around the dot
    "example . com",
    "example .com",
    "example. com",
    "example\t.\tcom",
    "example  .  net",
    # spelled out
    "example dot com",
    "example (dot) com",
    "example [dot] org",
    "example punkt com",
    # numbers
    "203.0.113.7",
    "203 . 0 . 113 . 7",
    "go to 203.0.113.7:8080",
    # punycode
    "xn--80ak6aa92e",
    "xn--e1afmkfd.example",
    "https://xn--80ak6aa92e.example.invalid",
    # full-width forms
    "ｅｘａｍｐｌｅ．ｃｏｍ",
    "ｈｔｔｐｓ：／／example.invalid",
    "example。com",
    # look-alike letters (Cyrillic a, o and Greek omicron)
    "exаmple.com",
    "example.cоm",
    "example.cοm",
    # invisible characters inside the name
    f"exa{ZWSP}mple.com",
    f"ex{ZWJ}ample.c{BOM}om",
    "exam­ple.com",
    "exaḿple.com",
    "example️.com",
    f"ht{ZWSP}tps://example.invalid",
    # tucked into a sentence
    "Klar, hier: example.com und mehr",
    "(example.com)",
    "<example.com>",
    "Danke!example.com",
]


@pytest.mark.parametrize("text", LINKS)
def test_a_link_is_refused(text: str) -> None:
    result = filter_outbound(text)
    assert result.blocked == BLOCKED_LINK
    assert result.text == ""


@pytest.mark.parametrize("text", ["Danke.Tschüss", "Hallo.Wie geht's", "ende.danke"])
def test_a_missing_space_after_a_full_stop_reads_as_a_domain(text: str) -> None:
    """The price of catching every ending: a line like this is refused rather than risked."""
    assert filter_outbound(text).blocked == BLOCKED_LINK


# -- chat commands ------------------------------------------------------------------------------

COMMANDS = [
    "/ban viewer_01",
    "/me waves",
    "/timeout viewer_01 600",
    ".ban viewer_01",
    ".timeout viewer_01",
    "!lurk",
    "!rps rock",
    "\\ban viewer_01",
    # padding and tricks around the first character
    "   /ban viewer_01",
    "\t/ban viewer_01",
    f"{ZWSP}/ban viewer_01",
    f"{ZWSP} {ZWJ}/me",
    f"{BOM}.ban",
    "/​ban viewer_01",
    " /ban viewer_01",
    "\x00/ban",
    "\x1b/ban",
    # full-width and look-alike slashes and dots
    "／ban viewer_01",
    "／me waves",
    "．ban viewer_01",
    "！lurk",
    "。ban viewer_01",
    "﹒ban viewer_01",
    # the dot-space-slash trick
    ". /ban viewer_01",
    ".  /me",
    "...",
    "...wirklich?",
]


@pytest.mark.parametrize("text", COMMANDS)
def test_a_line_that_starts_like_a_chat_command_is_refused(text: str) -> None:
    result = filter_outbound(text)
    assert result.blocked == BLOCKED_COMMAND_PREFIX
    assert result.text == ""


@pytest.mark.parametrize(
    "text",
    [
        "Bitte kein /ban hier",
        "Tippe !rps stein",
        "Hallo! Wie geht's?",
        "Danke. /me ist ein Befehl",
        "@viewer_01 /ban ist kein Befehl hier",
    ],
)
def test_a_command_word_further_in_is_no_command(text: str) -> None:
    assert filter_outbound(text).blocked == ""


# -- mentions -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@everyone aufgepasst", "everyone aufgepasst"),
        ("Hallo @EVERYONE!", "Hallo EVERYONE!"),
        ("Achtung @here und @channel und @all", "Achtung here und channel und all"),
        ("Hi ＠everyone", "Hi everyone"),
        ("Hi ﹫everyone", "Hi everyone"),
        ("Hi @ｅｖｅｒｙｏｎｅ", "Hi ｅｖｅｒｙｏｎｅ"),
        ("@ev​eryone", "everyone"),
    ],
)
def test_a_mass_mention_loses_its_at_sign(text: str, expected: str) -> None:
    result = filter_outbound(text)
    assert result.text == expected
    assert "mentions_capped" in result.changes or "invisible_or_white_space" in result.changes


def test_a_line_pings_at_most_the_allowed_number_of_people() -> None:
    names = [f"viewer_{i:02d}" for i in range(1, 6)]
    result = filter_outbound("Hi " + " ".join(f"@{n}" for n in names))
    assert result.text == "Hi @viewer_01 @viewer_02 viewer_03 viewer_04 viewer_05"
    assert result.text.count("@") == MAX_MENTIONS
    assert result.changes == ("mentions_capped",)


def test_a_few_mentions_are_left_alone() -> None:
    result = filter_outbound("@viewer_01 und @viewer_02, willkommen!")
    assert result.text == "@viewer_01 und @viewer_02, willkommen!"
    assert result.changes == ()


def test_a_mass_mention_does_not_use_up_the_mention_budget() -> None:
    result = filter_outbound("@everyone @viewer_01 @viewer_02")
    assert result.text == "everyone @viewer_01 @viewer_02"


# -- invisible characters, white space, length ----------------------------------------------------


def test_invisible_characters_are_removed_and_reported() -> None:
    result = filter_outbound(f"Hal{ZWSP}lo{ZWJ} Wel{BOM}t‎‮⁠️")
    assert result.text == "Hallo Welt"
    assert result.changes == ("invisible_or_white_space",)


def test_tag_characters_and_variation_selectors_are_removed() -> None:
    hidden = "".join(chr(0xE0000 + ord(c)) for c in "ban") + "\U000e0100"
    assert filter_outbound(f"ok{hidden}").text == "ok"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a\tb", "a b"),
        ("a\x07b", "a b"),
        ("a\x1bb", "a b"),
        ("a\x7fb", "a b"),
        ("a\u0085b", "a b"),
        ("a b", "a b"),
        ("a b", "a b"),
        ("a    b", "a b"),
        ("a 　b", "a b"),
        ("  a b  ", "a b"),
    ],
)
def test_control_characters_and_white_space_runs_become_one_space(text: str, expected: str) -> None:
    assert filter_outbound(text).text == expected


@pytest.mark.parametrize("text", ["", " ", "\t", ZWSP, f"{ZWSP} {BOM}", "\x00\x1b", "́"])
def test_a_line_with_nothing_visible_is_refused(text: str) -> None:
    result = filter_outbound(text)
    assert result.blocked == BLOCKED_EMPTY
    assert result.text == ""


def test_a_long_line_is_cut_to_the_limit_with_an_ellipsis() -> None:
    result = filter_outbound("wort " * 200)
    assert len(result.text) == MAX_OUTBOUND_CHARS
    assert result.text.endswith("wort…")
    assert result.changes == ("truncated",)


def test_a_line_at_the_limit_is_not_cut() -> None:
    text = "a" * MAX_OUTBOUND_CHARS
    result = filter_outbound(text)
    assert result.text == text
    assert result.changes == ()


def test_a_line_one_over_the_limit_is_cut() -> None:
    assert len(filter_outbound("a" * (MAX_OUTBOUND_CHARS + 1)).text) == MAX_OUTBOUND_CHARS


def test_what_is_sent_keeps_the_original_letters() -> None:
    """Detection folds a copy; the line itself is not rewritten (umlauts, ligatures, emoji)."""
    assert filter_outbound("Grüße aus Köln ﬁnal").text == "Grüße aus Köln ﬁnal"


def test_the_first_check_to_fire_names_the_reason() -> None:
    assert filter_outbound("/ban example.com").blocked == BLOCKED_COMMAND_PREFIX
    assert filter_outbound(f"{ZWSP}\n").blocked == BLOCKED_EMPTY
