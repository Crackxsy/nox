"""Text as it is said: written-out numbers, dates, times, units and abbreviations (German first).

Literal cases, one per rule - a wrong guess here is a word said wrongly, and the only protection
is that each rule has its examples. Anything the rules do not recognise stays as it is.
"""

from __future__ import annotations

import pytest

from nox.voice.tts.normalize import german_cardinal, german_ordinal, normalize_for_speech


@pytest.mark.parametrize(
    ("number", "words"),
    [
        (0, "null"),
        (1, "eins"),
        (7, "sieben"),
        (11, "elf"),
        (16, "sechzehn"),
        (17, "siebzehn"),
        (21, "einundzwanzig"),
        (30, "dreißig"),
        (99, "neunundneunzig"),
        (100, "einhundert"),
        (101, "einhunderteins"),
        (999, "neunhundertneunundneunzig"),
        (1000, "eintausend"),
        (1500, "eintausendfünfhundert"),
        (2026, "zweitausendsechsundzwanzig"),
        (21000, "einundzwanzigtausend"),
        (100000, "einhunderttausend"),
        (1000000, "eine Million"),
        (2500000, "zwei Millionen fünfhunderttausend"),
        (-4, "minus vier"),
    ],
)
def test_cardinal_numbers(number: int, words: str) -> None:
    assert german_cardinal(number) == words


def test_one_is_ein_in_front_of_a_noun() -> None:
    assert german_cardinal(1, ein=True) == "ein"
    assert german_cardinal(101, ein=True) == "einhundertein"
    assert german_cardinal(21, ein=True) == "einundzwanzig"


@pytest.mark.parametrize(
    ("number", "nominative", "dative"),
    [
        (1, "erste", "ersten"),
        (2, "zweite", "zweiten"),
        (3, "dritte", "dritten"),
        (7, "siebte", "siebten"),
        (8, "achte", "achten"),
        (9, "neunte", "neunten"),
        (12, "zwölfte", "zwölften"),
        (19, "neunzehnte", "neunzehnten"),
        (20, "zwanzigste", "zwanzigsten"),
        (22, "zweiundzwanzigste", "zweiundzwanzigsten"),
        (31, "einunddreißigste", "einunddreißigsten"),
    ],
)
def test_ordinal_numbers(number: int, nominative: str, dative: str) -> None:
    assert german_ordinal(number) == nominative
    assert german_ordinal(number, dative=True) == dative


@pytest.mark.parametrize(
    ("text", "said"),
    [
        # dates
        (
            "Heute ist der 22.11.2026.",
            "Heute ist der zweiundzwanzigste November zweitausendsechsundzwanzig.",
        ),
        ("Am 22.11. um neun.", "Am zweiundzwanzigsten November um neun."),
        ("Bis zum 1.1.2030 gilt es.", "Bis zum ersten Januar zweitausenddreißig gilt es."),
        ("Es war der 3. Oktober 1989.", "Es war der dritte Oktober neunzehnhundertneunundachtzig."),
        ("Wir treffen uns am 9. Mai.", "Wir treffen uns am neunten Mai."),
        ("Ab dem 15. März.", "Ab dem fünfzehnten März."),
        ("Den 4. Juli feiern wir.", "Den vierten Juli feiern wir."),
        # times
        ("Um 14:30 Uhr geht es los.", "Um vierzehn Uhr dreißig geht es los."),
        ("Um 9:05 ist es soweit.", "Um neun Uhr fünf ist es soweit."),
        ("Um 18:00 Uhr.", "Um achtzehn Uhr."),
        # money
        ("Das kostet 1.500 Euro.", "Das kostet eintausendfünfhundert Euro."),
        ("Das kostet 3,50 €.", "Das kostet drei Euro fünfzig."),
        ("Nur 1 Euro.", "Nur ein Euro."),
        ("Es sind 20$ pro Monat.", "Es sind zwanzig Dollar pro Monat."),
        # units
        ("Es sind 4 °C draußen.", "Es sind vier Grad Celsius draußen."),
        ("Zu 50 % fertig.", "Zu fünfzig Prozent fertig."),
        ("Mit 5 km/h.", "Mit fünf Kilometer pro Stunde."),
        ("Er wiegt 80 kg.", "Er wiegt achtzig Kilogramm."),
        ("Genau 1 km.", "Genau ein Kilometer."),
        ("Es hat 16 GB Speicher.", "Es hat sechzehn Gigabyte Speicher."),
        # decimals
        ("Pi ist 3,14.", "Pi ist drei Komma eins vier."),
        ("Es sind 0,5 Liter.", "Es sind null Komma fünf Liter."),
        # plain numbers
        ("Es sind 5 Minuten.", "Es sind fünf Minuten."),
        ("Windows 11 läuft.", "Windows elf läuft."),
        ("Rufnummer 0815 bitte.", "Rufnummer null acht eins fünf bitte."),
        (
            "Das sind 12345678901 Stück.",
            "Das sind eins zwei drei vier fünf sechs sieben acht neun null eins Stück.",
        ),
        # abbreviations
        ("Das ist z. B. ein Test.", "Das ist zum Beispiel ein Test."),
        ("Das ist z.B. ein Test.", "Das ist zum Beispiel ein Test."),
        ("Das heißt d. h. es geht.", "Das heißt das heißt es geht."),
        ("Dr. Meier kommt ca. 5 Minuten später.", "Doktor Meier kommt circa fünf Minuten später."),
        ("Prof. Lang und Hr. Kurz.", "Professor Lang und Hr. Kurz."),
        ("Äpfel, Birnen usw. sind gut.", "Äpfel, Birnen und so weiter sind gut."),
        ("Äpfel, Birnen usw. Dann Nüsse.", "Äpfel, Birnen und so weiter. Dann Nüsse."),
        ("Siehe Nr. 5.", "Siehe Nummer fünf."),
        ("Rund 3 Mio. Menschen.", "Rund drei Millionen Menschen."),
        # symbols
        ("Tee & Kaffee", "Tee und Kaffee"),
    ],
)
def test_german_text_is_written_out(text: str, said: str) -> None:
    assert normalize_for_speech(text, "de") == said


@pytest.mark.parametrize(
    "text",
    [
        "Hallo, ich bin Nox.",
        "H2O und mp3 und A4.",
        "1. Äpfel\n2. Birnen",
        "Die Version 2026-11-22 bleibt.",
        "Andr. und Lkw. bleiben",
        "",
    ],
)
def test_what_is_not_recognised_stays_as_it_was(text: str) -> None:
    assert normalize_for_speech(text, "de") == text


def test_a_second_pass_changes_nothing() -> None:
    text = "Am 9. Mai um 14:30 Uhr, z. B. 1.500 Euro, 4 °C, 50 %, ca. 3,5 kg, Dr. Meier usw."

    once = normalize_for_speech(text, "de")

    assert normalize_for_speech(once, "de") == once


def test_the_result_has_no_digits_left_where_the_rules_apply() -> None:
    said = normalize_for_speech("Am 22.11.2026 um 14:30 kostet es 1.500 Euro bei 4 °C.", "de")

    assert not any(c.isdigit() for c in said)


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("Pick one, e.g. this one.", "Pick one, for example this one."),
        ("Dr. Who is 50% done at 20 °C.", "Doctor Who is 50 percent done at 20 degrees Celsius."),
        ("Cats vs. dogs, etc. Then rain.", "Cats versus dogs, et cetera. Then rain."),
        ("Salt & pepper", "Salt and pepper"),
    ],
)
def test_english_gets_abbreviations_and_symbols(text: str, said: str) -> None:
    assert normalize_for_speech(text, "en") == said


def test_another_language_is_left_alone() -> None:
    assert normalize_for_speech("Le 9. mai, 5 km, z. B.", "fr") == "Le 9. mai, 5 km, z. B."


def test_the_language_may_carry_a_region() -> None:
    assert normalize_for_speech("5 Minuten", "de-DE") == "fünf Minuten"
