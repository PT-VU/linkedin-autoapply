# Compatibility launcher for the current AI scoring supervisor.
# The old prototype used a rule-based PowerShell scorer; the public workflow
# deliberately keeps scoring in the same Codex/API path as all other events.
$projectRoot = Split-Path -Parent $PSScriptRoot
python (Join-Path $projectRoot "scripts\codex_supervisor.py") --sandbox workspace-write
