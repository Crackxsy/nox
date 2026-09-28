"""The built-in privacy zones catch what they are for, and not the words that merely contain them.

A zone closes the microphone: a false positive is push-to-talk that silently does nothing in a
code editor with `datenbank.py` open. Both sides are tested - the intended windows still match.
"""

from __future__ import annotations

import pytest

from nox.security.privacy import BUILTIN_ZONES, PrivacyService, title_words


@pytest.fixture
def privacy() -> PrivacyService:
    return PrivacyService(zones=list(BUILTIN_ZONES))


@pytest.mark.parametrize(
    ("title", "zone"),
    [
        ("Sparkasse KölnBonn - Online-Banking - Firefox", "banking"),
        ("Meine Bank - Übersicht - Chrome", "banking"),
        ("Deutsche Bank - Login", "banking"),
        ("DKB Banking", "banking"),
        ("Commerzbank Banking - Edge", "banking"),
        ("N26 – Mein Konto", "banking"),
        ("PayPal: Zusammenfassung", "banking"),
        ("Trade Republic", "banking"),
        ("Depot - Übersicht - comdirect", "banking"),
        ("Finanzen 2026.xlsx - Excel", "banking"),
        ("KeePassXC", "password_manager"),
        ("Passwort ändern - Google Konto", "password_manager"),
        ("Reset your password - GitHub", "password_manager"),
        ("Posteingang - Outlook", "email"),
        ("WhatsApp", "private_chats"),
        ("Signal", "private_chats"),
        ("Telegram (3)", "private_chats"),
        ("Messenger | Facebook", "private_chats"),
        ("Steuererklärung 2025.pdf", "personal_documents"),
        ("Steuer 2025 - Belege.pdf", "personal_documents"),
        ("Befund Radiologie.pdf", "personal_documents"),
        ("#general - Discord", "discord"),
    ],
)
def test_intended_windows_are_zoned(privacy: PrivacyService, title: str, zone: str) -> None:
    assert privacy.match_zone(title, "chrome.exe") == zone


@pytest.mark.parametrize(
    "title",
    [
        "datenbank.py - nox - Visual Studio Code",
        "Datenbankmodell.drawio",
        "Bankverbindung_parser.rs - Neovim",
        "signal_handler.py - Visual Studio Code",
        "Signalverarbeitung – Vorlesung 3.pdf",
        "depot_tools - Terminal",
        "Motorsteuerung.cpp - CLion",
        "Steuerelement hinzufügen - Visual Studio",
        "password_reset.py - Visual Studio Code",
        "passwort_hash_test.go",
        "telegram_bot.py - PyCharm",
        "messenger_queue.rs - RustRover",
        "finanzen_api.ts - WebStorm",
        "Rocket League",
    ],
)
def test_words_that_merely_contain_a_zone_word_are_not_zoned(
    privacy: PrivacyService, title: str
) -> None:
    assert privacy.match_zone(title, "Code.exe") is None


@pytest.mark.parametrize(
    "path",
    ["C:/Projekte/steuerung/main.c", "/home/u/code/steuerungstechnik/notes.md"],
)
def test_path_patterns_do_not_catch_control_engineering(privacy: PrivacyService, path: str) -> None:
    assert privacy.path_zone(path) is None


@pytest.mark.parametrize(
    "path", ["/home/u/Dokumente/Steuer/2025/beleg.pdf", "C:/Users/u/steuererklärung_2025.pdf"]
)
def test_tax_paths_are_still_zoned(privacy: PrivacyService, path: str) -> None:
    assert privacy.path_zone(path) == "personal_documents"


def test_a_users_own_substring_pattern_keeps_its_meaning() -> None:
    privacy = PrivacyService(zones=[{"id": "work_db", "window_titles": ["*bank*"]}])
    assert privacy.match_zone("datenbank.py - VS Code") == "work_db"


def test_title_words_splits_on_everything_but_word_characters() -> None:
    assert title_words("Sparkasse: Online-Banking – Konto") == " sparkasse online banking konto "
    assert title_words("depot_tools") == " depot_tools "
