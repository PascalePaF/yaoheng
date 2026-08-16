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
    --distpath "dist" `
    --workpath "build" `
    --specpath "." `
    "main.py"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$StagedPortableDir = Join-Path "dist" $AppName
Copy-Item -LiteralPath $GuideName -Destination (Join-Path $StagedPortableDir $GuideName) -Force
Copy-Item -LiteralPath "app.ico" -Destination (Join-Path $StagedPortableDir "app.ico") -Force
Copy-Item -LiteralPath "app.png" -Destination (Join-Path $StagedPortableDir "app.png") -Force

# Build in an isolated staging folder first. Only after every test and build
# succeeds do we update the local portable copy, leaving its settings and data
# untouched. The ZIP is made from the clean staging output and contains no
# personal settings or cached market data.
$ReleaseRoot = Join-Path $ProjectDir "release"
$PortableDir = Join-Path $ReleaseRoot $AppName
New-Item -ItemType Directory -Path $PortableDir -Force | Out-Null
Copy-Item -Path (Join-Path $StagedPortableDir "*") -Destination $PortableDir -Recurse -Force

$ZipPath = Join-Path $ReleaseRoot ($AppName + "-绿色免安装版.zip")
Compress-Archive -LiteralPath $StagedPortableDir -DestinationPath $ZipPath -CompressionLevel Optimal -Force
Write-Host ""
Write-Host "Build complete: $ProjectDir\release\$AppName\$AppName.exe" -ForegroundColor Green
Write-Host "Portable ZIP:  $ZipPath" -ForegroundColor Green
