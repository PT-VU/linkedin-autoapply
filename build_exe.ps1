$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
  $python = "python"
}

$projectConfig = Join-Path $PSScriptRoot "config.yaml"
$portableConfig = Join-Path $PSScriptRoot "dist\LinkedInAutoApply\config.yaml"
if (-not (Test-Path -LiteralPath $projectConfig) -and (Test-Path -LiteralPath $portableConfig)) {
  Copy-Item -Force $portableConfig $projectConfig
}

$commonArgs = @(
  "--add-data", "src;src",
  "--add-data", "scripts;scripts",
  "--add-data", "main.py;.",
  "--collect-all", "yaml",
  "--collect-all", "playwright",
  "--collect-all", "pdfplumber",
  "--collect-all", "openpyxl",
  "--collect-all", "docx",
  "--hidden-import", "ctypes.wintypes"
)

& $python -m PyInstaller --noconfirm --clean --onedir --windowed `
  --name LinkedInAutoApply `
  @commonArgs `
  --add-data "config.example.yaml;." `
  desktop_launcher.py

& $python -m PyInstaller --noconfirm --clean --onedir --console `
  --name LinkedInAutoApplyWorker `
  @commonArgs `
  worker_entry.py

Copy-Item -Recurse -Force dist\LinkedInAutoApplyWorker\* dist\LinkedInAutoApply\
Copy-Item -Force config.example.yaml dist\LinkedInAutoApply\config.example.yaml
if (Test-Path -LiteralPath $projectConfig) {
  Copy-Item -Force $projectConfig dist\LinkedInAutoApply\config.yaml
}
New-Item -ItemType Directory -Force dist\LinkedInAutoApply\user_files | Out-Null
New-Item -ItemType Directory -Force dist\LinkedInAutoApply\data | Out-Null
New-Item -ItemType Directory -Force dist\LinkedInAutoApply\logs | Out-Null
New-Item -ItemType Directory -Force dist\LinkedInAutoApply\cover_letters | Out-Null

Write-Host "Built dist\LinkedInAutoApply\LinkedInAutoApply.exe"
