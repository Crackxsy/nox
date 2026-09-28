"""The security PIN on the command line: `nox pin set|clear|status` and the `nox onboard` step.

Both run the rules of `nox.security.pin_setup` - the same ones the dashboard uses - against the
real credential store. Two things are specific to a local terminal:

* the PIN is typed into a hidden prompt, twice, and never accepted as a command-line argument,
  where it would land in the shell history;
* an entry under `nox/security/pin` that is not a PIN hash (a raw value stored with
  `nox secrets set` before that was refused) can be replaced or removed here without the old PIN,
  because none can ever match it and the person at this terminal can edit the credential store
  directly anyway. The dashboard refuses the same change and points here.

Failed attempts are counted in the Nox database when it exists, so the lockout the core enforces
also holds for guesses typed here; before the first start there is no database and no PIN to
guess. A change made here is not written to the audit log: only the core appends to its hash
chain, and a second writer from another process would race it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import typer

from nox.security.pin_attempts import (
    InMemoryPinAttemptStore,
    PinAttemptStore,
    SqlitePinAttemptStore,
)
from nox.security.pin_setup import PinChangeError, PinSetup
from nox.security.secrets import (
    INVALID_PIN_ENTRY_REASON,
    MIN_PIN_LENGTH,
    PinEntryState,
    PinManager,
    PinPolicyError,
    SecretStoreUnavailableError,
    check_pin_policy,
)

__all__ = [
    "DATABASE_FILENAME",
    "PinPrompt",
    "cli_pin_clear",
    "cli_pin_set",
    "cli_pin_status",
    "local_pin_manager",
    "prompt_new_pin",
]

#: The core's database file inside `paths.database_dir`.
DATABASE_FILENAME = "nox.db"
#: How often a mistyped or mismatched new PIN is asked again before the step gives up.
NEW_PIN_TRIES = 3
#: How long a command waits for the core's database lock.
DB_TIMEOUT_S = 5.0

#: A hidden prompt: label -> the typed text (empty when the user just pressed Enter).
PinPrompt = Callable[[str], str]
Echo = Callable[[str], None]


def typer_pin_prompt(label: str) -> str:
    """The real hidden prompt: nothing typed is echoed, and Enter alone answers ''."""
    return str(typer.prompt(label, hide_input=True, default="", show_default=False))


@contextmanager
def local_pin_manager(database_dir: Path | None) -> Iterator[PinManager]:
    """A `PinManager` on the real credential store, counting attempts where the core does."""
    conn: sqlite3.Connection | None = None
    attempts: PinAttemptStore = InMemoryPinAttemptStore()
    db_path = database_dir / DATABASE_FILENAME if database_dir is not None else None
    if db_path is not None and db_path.exists():
        conn = sqlite3.connect(db_path, timeout=DB_TIMEOUT_S)
        attempts = SqlitePinAttemptStore(conn)
    from nox.security.secrets import KeyringSecretStore  # noqa: PLC0415 - keyring loads slowly

    try:
        yield PinManager(KeyringSecretStore(), attempts=attempts)
    finally:
        if conn is not None:
            conn.close()


def prompt_new_pin(prompt: PinPrompt, echo: Echo, texts: Mapping[str, str]) -> str | None:
    """Ask for a new PIN twice; `None` when the user leaves it empty or keeps mistyping.

    `texts` needs `pin_new`, `pin_repeat`, `pin_mismatch`, `pin_too_short` (with `{min}`) and
    `pin_gave_up`, so the wizard can ask in German and the command line in English.
    """
    for _ in range(NEW_PIN_TRIES):
        pin = prompt(texts["pin_new"])
        if not pin:
            return None
        try:
            check_pin_policy(pin)
        except PinPolicyError:
            echo(texts["pin_too_short"].format(min=MIN_PIN_LENGTH))
            continue
        if prompt(texts["pin_repeat"]) != pin:
            echo(texts["pin_mismatch"])
            continue
        return pin
    echo(texts["pin_gave_up"])
    return None


#: The command-line wording. English, like every other `nox` command's output.
CLI_TEXTS: dict[str, str] = {
    "pin_new": f"New PIN (at least {MIN_PIN_LENGTH} characters)",
    "pin_repeat": "Repeat the new PIN",
    "pin_mismatch": "The two entries differ; please try again.",
    "pin_current": "Current PIN",
    "pin_too_short": "The PIN needs at least {min} characters.",
    "pin_gave_up": "No PIN set.",
}


def cli_pin_status(pin: PinManager, echo: Echo) -> int:
    try:
        state = pin.entry_state()
    except SecretStoreUnavailableError as exc:
        echo(f"unavailable: {exc}")
        return 1
    if state is PinEntryState.INVALID:
        echo(f"invalid: {INVALID_PIN_ENTRY_REASON}")
        return 1
    locked_until = pin.locked_until
    line = "set" if state is PinEntryState.VALID else "not set"
    if locked_until is not None:
        line += f" (locked until {locked_until.isoformat(timespec='seconds')})"
    echo(line)
    return 0


def cli_pin_set(pin: PinManager, prompt: PinPrompt, echo: Echo) -> int:
    setup = PinSetup(pin)
    try:
        state = setup.state()
    except PinChangeError as exc:
        echo(exc.message)
        return 1
    current: str | None = None
    if state is PinEntryState.VALID:
        current = prompt(CLI_TEXTS["pin_current"])
    elif state is PinEntryState.INVALID:
        echo("The stored entry is not a Nox PIN hash; it will be replaced.")
    new_pin = prompt_new_pin(prompt, echo, CLI_TEXTS)
    if new_pin is None:
        return 1
    try:
        asyncio.run(setup.set(new_pin, current_pin=current, by="cli", allow_invalid_entry=True))
    except PinChangeError as exc:
        echo(exc.message)
        return 1
    echo(f"PIN set ({pin.algorithm}).")
    return 0


def cli_pin_clear(pin: PinManager, prompt: PinPrompt, echo: Echo) -> int:
    setup = PinSetup(pin)
    try:
        state = setup.state()
    except PinChangeError as exc:
        echo(exc.message)
        return 1
    current = prompt(CLI_TEXTS["pin_current"]) if state is PinEntryState.VALID else None
    try:
        asyncio.run(setup.clear(current_pin=current, by="cli", allow_invalid_entry=True))
    except PinChangeError as exc:
        echo(exc.message)
        return 1
    echo("PIN removed.")
    return 0
