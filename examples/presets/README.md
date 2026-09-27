# Preset examples

Working starting points for the **Programs** section of the Presets tab. Copy one, change the
parts marked `CHANGE ME`, and register it as an action.

Nothing here is installed or run by Nox on its own. A file becomes reachable only once you enter
its path yourself, and even then only the preset you attach it to can start it.

| File | What it does | Needs |
| --- | --- | --- |
| `set-mouse-dpi-v1.ahk` | sends the key combination you bound to a DPI step in your mouse software | [AutoHotkey 1.1](https://www.autohotkey.com/) |
| `set-mouse-dpi-v2.ahk` | the same, for AutoHotkey 2.0 | [AutoHotkey 2.0](https://www.autohotkey.com/) |
| `focus-quiet.ps1` | suppresses notification banners for a focus preset | Windows PowerShell 5.1 |

Both AutoHotkey versions are in wide use and their syntax is not compatible, so take the file that
matches yours: right-click the tray icon, or run `AutoHotkey.exe /?`. A 1.1 script started with a
2.0 interpreter fails immediately, which the Test button will show you.

## Registering one

1. Save the file somewhere permanent - **not** in Downloads, where it will be cleaned up.
2. On the Presets tab, under **Programs**, press *Register a program*.
3. Name it, then give the full path of the interpreter as the program and the script as its first
   argument:
   - AutoHotkey 1.1: program `C:/Program Files/AutoHotkey/AutoHotkey.exe`, arguments the `.ahk`
     path and then `low` or `high`.
   - AutoHotkey 2.0: program `C:/Program Files/AutoHotkey/v2/AutoHotkey64.exe`, same arguments.
   - PowerShell: program `C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe`, arguments
     `-NoProfile`, `-ExecutionPolicy`, `Bypass`, `-File`, then the `.ps1` path.
4. Press **Test**. A wrong path fails here in a second, rather than silently in the middle of a
   preset later.

## Why a script and not a driver

Nox has no built-in support for any mouse, keyboard or headset, and that is on purpose: vendor
tools change their internals without notice, and an integration built on those internals breaks
silently - usually at the moment you were relying on it. A script you can read, run by hand, and
fix is worth more than a binding that works until the next update.
