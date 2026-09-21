"""Nox desktop shell: PySide6 pet window, tray, global hotkeys and native dialogs.

The shell is an IPC client of the core (vault 02 - Architecture/Process Model, "Shell"). It renders
the pet via QWebEngineView pointing at the core's HTTP server, owns tray/hotkeys/dialogs and
forwards every action as a typed IPC request. It never decides anything on its own except the kill
switch second path to the supervisor.
"""
