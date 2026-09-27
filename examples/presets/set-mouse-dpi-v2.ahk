; Switches mouse sensitivity by sending the key combination your mouse software listens for.
;
; This is the route that works with vendor software offering no command line of its own -
; Logitech G HUB, Razer Synapse, Corsair iCUE all allow a DPI step to be bound to a hotkey, and
; a hotkey can be sent from here.
;
; Requires AutoHotkey v2.
;
; Setup, once:
;   1. In your mouse software, bind "DPI up", "DPI down" or a named DPI stage to a key
;      combination nothing else uses. Ctrl+Alt+F9 and Ctrl+Alt+F10 are good candidates.
;   2. Put the same combinations below.
;   3. Run this file by hand once and check that the sensitivity really changes. If it does not,
;      the vendor software is not listening for synthetic input - see the README for the
;      per-application profile route instead.
;
; Usage from a preset, as the first argument after the script path:
;   low   - the gaming sensitivity
;   high  - the desktop sensitivity
;
; Exit codes: 0 = sent, 2 = no or unknown argument. Nox reports a non-zero code on the card,
; so a typo here is visible rather than silent.

#Requires AutoHotkey v2.0
#SingleInstance Force

; CHANGE ME - the combinations you bound in your mouse software.
;   ^ = Ctrl, ! = Alt, + = Shift, # = Windows
LOW_DPI_HOTKEY  := "^!F9"
HIGH_DPI_HOTKEY := "^!F10"

; Some vendor tools ignore input that arrives faster than a human could produce it.
SendMode("Event")
SetKeyDelay(40, 40)

if A_Args.Length < 1 {
    ExitApp(2)
}

switch StrLower(A_Args[1]) {
    case "low":
        Send(LOW_DPI_HOTKEY)
    case "high":
        Send(HIGH_DPI_HOTKEY)
    default:
        ExitApp(2)
}

ExitApp(0)
