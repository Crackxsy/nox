"""Settings area (EPIC-21): the editable half of the dashboard's Settings page.

`config.effective` (in `nox.app`) has always been read-only - "in den Einstellungen kann man nur
den Status lesen". This package adds the writing half, all of it behind the normal IPC registry
with `shell`/`dashboard` roles and the normal audit trail:

* `config.get` / `config.set` - a small allow-list of configuration paths, whose types, options
  and bounds are derived from the pydantic models in `nox.core.config` rather than hand-kept
  (`nox.settings.schema`), written into the User layer (`user.yaml`) by the same writer
  `nox onboard` uses (`nox.settings.layers`).
* `secrets.status` / `secrets.set` / `secrets.delete` - the known secret *names* only; values go
  straight into the OS keyring, are never logged, never echoed and never audited
  (`nox.settings.secrets_ipc`).
* `twitch.auth.*` - a Twitch OAuth Device Code login so the user never has to paste a token
  (`nox.settings.twitch_auth`).
* `personality.get` / `personality.set` - the character text, which lives in
  `<data_dir>/personality.md`, not in the source tree (`nox.settings.personality`).

`install(core)` wires all of it onto a booted `NoxCore`; see `nox.settings.install`.
"""
