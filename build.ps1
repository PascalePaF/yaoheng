$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir
$AppName = -join @([char]26332, [char]34913)
$GuideName = (-join @([char]20351, [char]29992, [char]35828, [char]26126)) + ".txt"

Write-Host "[1/4] Checking Python packages..."
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed with exit code $LASTEXITCODE"
}

Write-Host "[2/4] Creating application icon..."
python make_icon.py
if ($LASTEXITCODE -ne 0) {
    throw "Icon generation failed with exit code $LASTEXITCODE"
}

Write-Host "[3/4] Running automated tests..."
python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    throw "Automated tests failed with exit code $LASTEXITCODE"
}

Write-Host "[4/4] Building portable executable..."
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name $AppName `
    --icon "app.ico" `
    --collect-all "tzdata" `
    --distpath "release" `
    --workpath "build" `
    --specpath "." `
    "main.py"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$PortableDir = Join-Path "release" $AppName
Copy-Item -LiteralPath $GuideName -Destination (Join-Path $PortableDir $GuideName) -Force
Copy-Item -LiteralPath "app.ico" -Destination (Join-Path $PortableDir "app.ico") -Force
Copy-Item -LiteralPath "app.png" -Destination (Join-Path $PortableDir "app.png") -Force
if (Test-Path -LiteralPath "app_settings.json") {
    Copy-Item -LiteralPath "app_settings.json" -Destination (Join-Path $PortableDir "app_settings.json") -Force
}
if (Test-Path -LiteralPath "data") {
    $PortableData = Join-Path $PortableDir "data"
    New-Item -ItemType Directory -Path $PortableData -Force | Out-Null
    Copy-Item -Path "data\*" -Destination $PortableData -Recurse -Force
}
Write-Host ""
Write-Host "Build complete: $ProjectDir\release\$AppName\$AppName.exe" -ForegroundColor Green
