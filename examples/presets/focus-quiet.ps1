<#
.SYNOPSIS
    Suppresses Windows notification banners for a focus preset.

.DESCRIPTION
    Sets the same registry value the Settings app writes for "Notifications", so the change is the
    one Windows itself makes rather than a simulated key press. Running the script with -Off puts
    it back.

    What it deliberately does NOT do is mute other applications. There is no supported way to set
    another application's volume from a script, and a script that lowered the master volume while
    claiming to mute other applications would be worse than one that says so plainly. Use the
    volume mixer, or a Home Assistant scene for the speakers in the room.

    Written for Windows PowerShell 5.1, which is what `powershell.exe` is on a stock Windows - no
    PowerShell 7 syntax, so it runs wherever it is registered.

.PARAMETER Off
    Restores notification banners instead of suppressing them.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File focus-quiet.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File focus-quiet.ps1 -Off

.NOTES
    Exit codes: 0 = applied, 1 = the setting could not be written. Nox shows a non-zero code on
    the card, so a failure here is visible rather than silent.
#>

[CmdletBinding()]
param(
    [switch]$Off
)

$ErrorActionPreference = 'Stop'

$key = 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\PushNotifications'
$name = 'ToastEnabled'

# The value is the inverse of the switch: toasts enabled means notifications are showing.
if ($Off) {
    $value = 1
    $message = 'Notification banners on.'
} else {
    $value = 0
    $message = 'Notification banners suppressed.'
}

try {
    if (-not (Test-Path $key)) {
        New-Item -Path $key -Force | Out-Null
    }
    Set-ItemProperty -Path $key -Name $name -Value $value -Type DWord
    Write-Output $message
    exit 0
}
catch {
    Write-Error "Could not change the notification setting: $_"
    exit 1
}
