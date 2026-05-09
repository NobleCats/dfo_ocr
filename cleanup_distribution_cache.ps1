# Remove local development/user cache before packaging or testing a clean install.
# This deletes the saved GUI API key from QSettings/registry, local debug logs,
# captured last_frame images, and local PyInstaller output.

$ErrorActionPreference = "SilentlyContinue"

Write-Host "Removing DFOGANG Raid Helper local cache..."

Remove-Item -Path "HKCU:\Software\DFOGANG\RaidHelper" -Recurse -Force
Remove-Item -Path "$env:LOCALAPPDATA\DFOGANG_RaidHelper" -Recurse -Force

Remove-Item -Path ".\build" -Recurse -Force
Remove-Item -Path ".\dist" -Recurse -Force
Remove-Item -Path ".\DFOGANG_RaidHelper.spec" -Force

Write-Host "Done."
