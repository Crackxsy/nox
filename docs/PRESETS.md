# Presets

A preset is a name, the ways it can start, and an ordered list of steps. Saying one sentence can
dim the lights, switch Nox's own mode, and start a program you registered - together, in one go.

Everything below is edited on the dashboard's **Presets** tab. Nothing here is on by default: a
fresh installation has no presets and no programs, and can therefore run nothing at all.

## What a step can do

| Step | What it does | Needs |
| --- | --- | --- |
| Light | on/off, brightness, colour temperature | Home Assistant |
| Socket | on/off | Home Assistant |
| Scene | activates a scene you defined in Home Assistant | Home Assistant |
| Heating | sets a target temperature | Home Assistant |
| Nox mode | switches how loudly and how often Nox speaks | — |
| Start a program | runs one program you registered | — |
| Say | Nox says one sentence | — |
| Wait | pauses before the next step | — |

Steps run in order. **A preset is not a transaction:** if the lamp is unplugged, that step is
reported and the remaining steps still run, so the program still starts. Nox says which step did
not work rather than claiming the whole preset failed - or, worse, that it worked.

## What can start a preset

* **A phrase.** "Gaming mode" is matched before any language model is asked, in microseconds.
  Talking *about* a preset does not activate it: a question mark, a question word at the start
  ("was", "kannst", "how", …) or a sentence longer than six words goes to the model instead.
* **A time of day**, optionally limited to certain weekdays.
* **A program starting or ending** - `RocketLeague.exe`, say. The program has to be listed in
  `sensors.game.process_names` as well, which is what makes Nox watch for it.
* **The dashboard**, with the Run button.
* **Nox itself**, when you ask it to in words it did not recognise as a phrase. It can only name a
  preset; it can never assemble one.

## Registering a program

Under **Programs** you give a program a name and a full path. Nox may start exactly what is listed
there and nothing else:

* The language model cannot pass a command line, a path or an argument. It can ask for a preset by
  name, and your configuration decides what that means.
* The program is started without a shell, so an argument stays an argument even when it contains
  spaces or an `&`.
* The path has to be absolute. A bare name would be looked up in `PATH` and could mean different
  programs depending on how Nox happened to start.
* Every start goes through the same permission check and the same audit entry as any other tool
  call, and stops with the kill switch.

Use **Test** after adding one. A path that moved or a script that needs its own working directory
is the kind of mistake only a real start finds.

## Mouse sensitivity, lighting and other hardware

Nox has no built-in support for any mouse, keyboard or headset. It starts programs, and the
program you point it at does the device-specific part. That is deliberate: vendor tools change
their internals without notice, and an integration built on those internals breaks silently.

What works today, with any hardware:

1. Make the change once with the tool you already have - a profile, a macro, a small script.
2. Find a way to trigger it from the command line. Most vendor tools can export a profile, and
   [AutoHotkey](https://www.autohotkey.com/) can drive almost anything that has a hotkey.
3. Register that command as a program, test it, and add a **Start a program** step.

### Logitech G HUB

G HUB has no public command line interface, so a preset cannot ask it for a DPI change directly.
Two approaches work in practice:

* **Per-application profiles.** G HUB switches profile by itself when a given executable is in the
  foreground. Set the DPI you want in the profile bound to your game, and the switch happens
  without Nox at all - a preset then only has to handle the lights and the mode.
* **A hotkey plus a script.** Bind a DPI step to a key combination inside G HUB, then register an
  AutoHotkey script that sends that combination. This is the route that gives a preset control,
  and it is also the fragile one: G HUB has to be running and focused input has to reach it.

Neither route is verified in this repository - there is no Logitech hardware in the test suite. If
you get one working, the Test button tells you so in a second, which is why it is there.

## Where presets are stored

In `config/user.yaml`, under `presets`, written through the same validated path as every other
setting. That means:

* A preset the configuration rejects is refused on the dashboard, with the reason, instead of
  being stored in a shape that would quietly do nothing.
* Changes take effect immediately. No restart.
* The audit log records that `presets.items` changed - never what it changed to, because a preset
  can carry a room name or a path.

`config/defaults.yaml` has a commented example of a complete preset.
