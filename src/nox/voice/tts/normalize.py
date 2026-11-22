"""Text as it is said: numbers, dates, times, units and abbreviations written out for the voice.

A speech engine reads what it is given. "22.11.2026" becomes a string of digits or the wrong
words, "1.500 Euro" is read as one-point-five-hundred, "4 °C" loses its unit, and "z. B." is read
as two letters - and as a sentence end, with the pause and falling voice that come with it. This
module turns such text into plain words before it reaches the engine, in German (the default
language) and, for abbreviations and the common symbols, in English.

It is deterministic and local: a table of abbreviations, a few patterns and the rules for German
number words. No model, no network, no state. What is not recognised is left exactly as it was, so
a wrong guess costs a mispronunciation, never lost text. The text shown to the user and the words
sent over the wire are not touched: only what the engine is asked to say is normalised
(`nox.voice.pipeline`), and a request can opt out (`TtsRequest.normalize`).

The grammar is the simple kind: "am 9. Mai" is "am neunten Mai" (an ordinal after am, vom, zum,
dem, ... takes -en, after anything else -e), a unit after exactly one is singular, every other
number takes the plural form. Numbers with a leading zero, and long digit strings (telephone and
account numbers), are read digit by digit.
"""

from __future__ import annotations

import re

__all__ = ["german_cardinal", "german_ordinal", "normalize_for_speech"]

_UNITS = (
    "null ein zwei drei vier fünf sechs sieben acht neun zehn elf zwölf dreizehn vierzehn "
    "fünfzehn sechzehn siebzehn achtzehn neunzehn"
).split()
_TENS = {2: "zwanzig", 3: "dreißig", 4: "vierzig", 5: "fünfzig", 6: "sechzig", 7: "siebzig"}
_TENS |= {8: "achtzig", 9: "neunzig"}
_ORDINAL_STEMS = {
    1: "erst",
    2: "zweit",
    3: "dritt",
    4: "viert",
    5: "fünft",
    6: "sechst",
    7: "siebt",
    8: "acht",
}
#: Digit strings longer than this are an identifier, not a quantity.
_MAX_QUANTITY_DIGITS = 9


def _below_hundred(n: int, *, ein: bool) -> str:
    if n < 20:
        return ("eins" if n == 1 and not ein else _UNITS[n]) if n else "null"
    unit, ten = n % 10, n // 10
    if unit == 0:
        return _TENS[ten]
    return f"{_UNITS[unit]}und{_TENS[ten]}"


def _below_thousand(n: int, *, ein: bool) -> str:
    hundreds, rest = divmod(n, 100)
    out = f"{_UNITS[hundreds]}hundert" if hundreds else ""
    if rest or not hundreds:
        out += _below_hundred(rest, ein=ein)
    return out


def german_cardinal(n: int, *, ein: bool = False) -> str:
    """`n` in German words: 1500 -> "eintausendfünfhundert". With `ein`, 1 is "ein" ("ein Euro")
    instead of "eins"."""
    if n < 0:
        return "minus " + german_cardinal(-n, ein=ein)
    if n == 0:
        return "null"
    parts: list[str] = []
    millions, rest = divmod(n, 1_000_000)
    if millions:
        words = german_cardinal(millions, ein=True)
        parts.append("eine Million " if millions == 1 else f"{words} Millionen ")
    thousands, rest = divmod(rest, 1000)
    if thousands:
        parts.append(f"{_below_thousand(thousands, ein=True)}tausend")
    if rest:
        parts.append(_below_thousand(rest, ein=ein))
    return "".join(parts).strip()


def german_ordinal(n: int, *, dative: bool = False) -> str:
    """`n` as an ordinal: 3 -> "dritte" ("dritten" with `dative`, after am, zum, vom, ...)."""
    ending = "en" if dative else "e"
    if n in _ORDINAL_STEMS:
        return _ORDINAL_STEMS[n] + ending
    if n < 20:
        return german_cardinal(n) + "t" + ending
    if n < 100 or n % 100 == 0 or n % 1000 == 0:
        return german_cardinal(n) + "st" + ending
    tail = n % 100
    head = n - tail
    if tail < 20 and tail:
        return german_cardinal(head) + german_ordinal(tail, dative=dative)
    return german_cardinal(n) + "st" + ending


def _digits(text: str) -> str:
    """Digit by digit: "0815" -> "null acht eins fünf"."""
    return " ".join("eins" if c == "1" else _UNITS[int(c)] for c in text)


def _number(text: str, *, ein: bool = False) -> str:
    """A run of digits as words; identifiers (leading zero, very long) digit by digit."""
    if (len(text) > 1 and text.startswith("0")) or len(text) > _MAX_QUANTITY_DIGITS:
        return _digits(text)
    return german_cardinal(int(text), ein=ein)


def _year(n: int) -> str:
    if 1100 <= n <= 1999:
        hundreds, rest = divmod(n, 100)
        return german_cardinal(hundreds) + "hundert" + (german_cardinal(rest) if rest else "")
    return german_cardinal(n)


_MONTH_NAMES = (
    "januar februar märz april mai juni juli august september oktober november dezember"
).split()
#: Words after which an ordinal takes the dative ending: "am neunten Mai".
_DATIVE_BEFORE = r"(?i:am|vom|zum|im|dem|ab|bis|seit|von|vor|nach|gegen|an|auf|um|zu|bei)"

_ABBREVIATIONS_DE: dict[str, str] = {
    "z. B.": "zum Beispiel",
    "z.B.": "zum Beispiel",
    "z. T.": "zum Teil",
    "z.T.": "zum Teil",
    "d. h.": "das heißt",
    "d.h.": "das heißt",
    "u. a.": "unter anderem",
    "u.a.": "unter anderem",
    "i. d. R.": "in der Regel",
    "i.d.R.": "in der Regel",
    "o. ä.": "oder ähnliches",
    "o.ä.": "oder ähnliches",
    "usw.": "und so weiter",
    "bzw.": "beziehungsweise",
    "bspw.": "beispielsweise",
    "ggf.": "gegebenenfalls",
    "evtl.": "eventuell",
    "vgl.": "vergleiche",
    "inkl.": "inklusive",
    "zzgl.": "zuzüglich",
    "etc.": "et cetera",
    "ca.": "circa",
    "Dr.": "Doktor",
    "Prof.": "Professor",
    "Nr.": "Nummer",
    "Mio.": "Millionen",
    "Mrd.": "Milliarden",
    "max.": "maximal",
    "sog.": "sogenannte",
}
_ABBREVIATIONS_EN: dict[str, str] = {
    "e.g.": "for example",
    "i.e.": "that is",
    "etc.": "et cetera",
    "vs.": "versus",
    "Dr.": "Doctor",
    "Mr.": "Mister",
    "Mrs.": "Missus",
    "Prof.": "Professor",
    "approx.": "approximately",
}

#: unit symbol -> (singular, plural). Case matters ("m" metre, "M" is not a unit here).
_UNIT_WORDS_DE: dict[str, tuple[str, str]] = {
    "km/h": ("Kilometer pro Stunde", "Kilometer pro Stunde"),
    "km": ("Kilometer", "Kilometer"),
    "kg": ("Kilogramm", "Kilogramm"),
    "mm": ("Millimeter", "Millimeter"),
    "cm": ("Zentimeter", "Zentimeter"),
    "ml": ("Milliliter", "Milliliter"),
    "GB": ("Gigabyte", "Gigabyte"),
    "MB": ("Megabyte", "Megabyte"),
    "kB": ("Kilobyte", "Kilobyte"),
    "TB": ("Terabyte", "Terabyte"),
    "Hz": ("Hertz", "Hertz"),
    "m": ("Meter", "Meter"),
    "g": ("Gramm", "Gramm"),
    "l": ("Liter", "Liter"),
    "€": ("Euro", "Euro"),
    "$": ("Dollar", "Dollar"),
    "%": ("Prozent", "Prozent"),
    "°C": ("Grad Celsius", "Grad Celsius"),
    "°F": ("Grad Fahrenheit", "Grad Fahrenheit"),
    "°": ("Grad", "Grad"),
}
_UNIT_PATTERN = "|".join(re.escape(u) for u in sorted(_UNIT_WORDS_DE, key=len, reverse=True))
_WORD_UNITS_DE = {"Euro": "Euro", "EUR": "Euro", "Dollar": "Dollar", "USD": "Dollar"}

_NUM = r"\d{1,3}(?:\.\d{3})+|\d+"
_DATE = re.compile(
    rf"(?<![\d.])(?P<prep>{_DATIVE_BEFORE}\s+)?(?:(?P<art>(?i:der|den|dem))\s+)?"
    r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4}|\d{2})?(?![\d])"
)
_ORDINAL_MONTH = re.compile(
    rf"(?<![\d.])(?P<prep>{_DATIVE_BEFORE}\s+)?(?:(?P<art>(?i:der|den|dem))\s+)?"
    rf"(?P<d>\d{{1,2}})\.\s+(?P<month>{'|'.join(m.capitalize() for m in _MONTH_NAMES)})\b"
    r"(?:\s+(?P<y>\d{4})(?!\d))?"
)
_TIME = re.compile(r"(?<![\d:.])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?:\s*Uhr)?(?![\d:])")
_MONEY = re.compile(
    r"(?<![\d.,])(?P<int>"
    + _NUM
    + r")(?:,(?P<dec>\d{1,2}))?\s*(?P<cur>€|\$|Euro|EUR|Dollar|USD)(?![A-Za-zäöüÄÖÜ])"
)
_DECIMAL = re.compile(r"(?<![\d.,])(?P<int>" + _NUM + r"),(?P<dec>\d+)(?![\d])")
_WITH_UNIT = re.compile(
    r"(?<![\d.,])(?P<num>" + _NUM + r")\s?(?P<unit>" + _UNIT_PATTERN + r")(?![A-Za-zäöüÄÖÜ0-9])"
)
_PLAIN_NUMBER = re.compile(r"(?<![\w.,/:-])(?P<num>" + _NUM + r")(?!\w|[.,]\d|-\d)")


def _to_int(text: str) -> int:
    return int(text.replace(".", ""))


def _date(m: re.Match[str]) -> str:
    prep, art = m.group("prep") or "", m.group("art") or ""
    day, month = int(m["d"]), int(m["m"])
    if not 1 <= day <= 31 or not 1 <= month <= 12:
        return m.group(0)
    dative = bool(prep) or art.lower() in {"dem"}
    if art.lower() == "den":
        dative = True
    words = f"{german_ordinal(day, dative=dative)} {_MONTH_NAMES[month - 1].capitalize()}"
    year = m.group("y")
    if year:
        words += " " + _year(int(year) if len(year) == 4 else 2000 + int(year))
    return f"{prep}{art + ' ' if art else ''}{words}"


def _ordinal_month(m: re.Match[str]) -> str:
    prep, art = m.group("prep") or "", m.group("art") or ""
    dative = bool(prep) or art.lower() in {"dem", "den"}
    words = f"{german_ordinal(int(m['d']), dative=dative)} {m['month']}"
    if m["y"]:
        words += " " + _year(int(m["y"]))
    return f"{prep}{art + ' ' if art else ''}{words}"


def _time(m: re.Match[str]) -> str:
    hours, minutes = int(m["h"]), int(m["m"])
    out = f"{german_cardinal(hours)} Uhr"
    return out + (f" {german_cardinal(minutes)}" if minutes else "")


def _money(m: re.Match[str]) -> str:
    amount = _to_int(m["int"])
    cur = _WORD_UNITS_DE.get(m["cur"]) or _UNIT_WORDS_DE[m["cur"]][0]
    out = f"{german_cardinal(amount, ein=True)} {cur}"
    if m["dec"]:
        cents = int(m["dec"].ljust(2, "0"))
        if cents:
            out += f" {german_cardinal(cents, ein=True)}"
    return out


def _decimal(m: re.Match[str]) -> str:
    integer = german_cardinal(_to_int(m["int"]))
    return f"{integer} Komma {_digits(m['dec'])}"


def _with_unit(m: re.Match[str]) -> str:
    number = _to_int(m["num"])
    singular, plural = _UNIT_WORDS_DE[m["unit"]]
    return f"{german_cardinal(number, ein=True)} {singular if number == 1 else plural}"


def _plain_number(m: re.Match[str]) -> str:
    text, start = m.string, m.start()
    opens_a_line = start == 0 or text[start - 1] == "\n"
    if opens_a_line and text.startswith(". ", m.end()):
        return m.group(0)  # "1. Äpfel": a list marker, left for the engine as it is
    return _number(m["num"].replace(".", ""), ein=False)


#: Abbreviations that are as often the last word of a sentence: the full stop is kept there.
_MAY_END_A_SENTENCE = frozenset({"usw.", "etc.", "u. a.", "u.a.", "o. ä.", "o.ä.", "e.g.", "i.e."})


def _abbreviations(text: str, table: dict[str, str]) -> str:
    for short in sorted(table, key=len, reverse=True):
        # Not inside a longer word ("Lkw.", "Dr." in "Andr."): the abbreviation stands alone.
        pattern = r"(?<![\wÄÖÜäöü.])" + re.escape(short).replace(r"\ ", r"\s*") + r"(?![\wÄÖÜäöü])"

        def spoken(m: re.Match[str], short: str = short) -> str:
            words = table[short]
            after = m.string[m.end() :].lstrip()
            if short in _MAY_END_A_SENTENCE and (not after or after[:1].isupper()):
                return words + "."  # it ended the sentence: keep the stop (and the pause)
            return words

        text = re.sub(pattern, spoken, text)
    return text


def _german(text: str) -> str:
    text = _abbreviations(text, _ABBREVIATIONS_DE)
    text = _DATE.sub(_date, text)
    text = _ORDINAL_MONTH.sub(_ordinal_month, text)
    text = _TIME.sub(_time, text)
    text = _MONEY.sub(_money, text)
    text = _DECIMAL.sub(_decimal, text)
    text = _WITH_UNIT.sub(_with_unit, text)
    text = _PLAIN_NUMBER.sub(_plain_number, text)
    return text.replace(" & ", " und ")


def _english(text: str) -> str:
    text = _abbreviations(text, _ABBREVIATIONS_EN)
    text = re.sub(r"(?<=\d)\s?°C", " degrees Celsius", text)
    text = re.sub(r"(?<=\d)\s?%", " percent", text)
    return text.replace(" & ", " and ")


def normalize_for_speech(text: str, language: str = "de") -> str:
    """`text` as the engine should be given it. Unknown languages are returned unchanged."""
    lang = language.lower()[:2]
    if lang == "de":
        return _german(text)
    if lang == "en":
        return _english(text)
    return text
