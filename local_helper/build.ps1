$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv/Scripts/python.exe"
Push-Location $root
try {
    & $python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name VideoInsightLoginHelper --paths $root `
        --distpath "web/downloads" --workpath "output/helper-build" `
        --specpath "output/helper-build" --exclude-module pytest --exclude-module numpy `
        "local_helper/main.py"
    if ($LASTEXITCODE -ne 0) { throw "Helper build failed." }
} finally {
    Pop-Location
}
