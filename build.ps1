$ErrorActionPreference = "Stop"
$ProjectDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$AppName = -join @([char]26332, [char]34913)
$AppVersion = "3.16"
$GuideName = (-join @([char]20351, [char]29992, [char]35828, [char]26126)) + ".txt"
$PathSeparators = [char[]]@(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)

function Assert-SafeChildPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$AllowedRoot
    )

    $RootFull = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd($PathSeparators)
    $PathFull = [System.IO.Path]::GetFullPath($Path)
    $ExpectedPrefix = $RootFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $PathFull.StartsWith($ExpectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to use a build path outside the allowed directory: $PathFull"
    }

    # A lexical child path can still escape through a junction or symbolic link.
    # Reject every existing reparse point from the allowed root down to the target.
    $Candidates = @($RootFull)
    $RelativePath = $PathFull.Substring($ExpectedPrefix.Length)
    $CurrentPath = $RootFull
    foreach ($Part in ($RelativePath -split '[\\/]')) {
        if ($Part) {
            $CurrentPath = Join-Path $CurrentPath $Part
            $Candidates += $CurrentPath
        }
    }
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate) {
            $Item = Get-Item -LiteralPath $Candidate -Force
            if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to use a reparse point as a build path: $Candidate"
            }
        }
    }

    return $PathFull
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$PythonArguments,

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    & $PythonPath @PythonArguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

function Find-InnoCompiler {
    $Candidates = @()
    if ($env:YAO_HENG_ISCC) {
        $Candidates += $env:YAO_HENG_ISCC
    }
    $Candidates += @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )

    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            return [System.IO.Path]::GetFullPath($Candidate)
        }
    }

    $Command = Get-Command "ISCC.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Command) {
        return $Command.Source
    }

    throw "Inno Setup 6 was not found. Install JRSoftware.InnoSetup with winget, or set YAO_HENG_ISCC to ISCC.exe."
}

$PythonCommand = Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1
$BootstrapPythonPath = $PythonCommand.Source
$PythonPath = $BootstrapPythonPath
$DistRoot = Assert-SafeChildPath -Path (Join-Path $ProjectDir "dist") -AllowedRoot $ProjectDir
$BuildRoot = Assert-SafeChildPath -Path (Join-Path $ProjectDir "build") -AllowedRoot $ProjectDir
$BuildEnvRoot = Assert-SafeChildPath -Path (Join-Path $ProjectDir ".venv-build") -AllowedRoot $ProjectDir
$ReleaseRoot = Assert-SafeChildPath -Path (Join-Path $ProjectDir "release") -AllowedRoot $ProjectDir
$StagedPortableDir = Assert-SafeChildPath -Path (Join-Path $DistRoot $AppName) -AllowedRoot $DistRoot
$PortableDir = Assert-SafeChildPath -Path (Join-Path $ReleaseRoot $AppName) -AllowedRoot $ReleaseRoot
$PortableExecutable = Assert-SafeChildPath -Path (Join-Path $PortableDir ($AppName + ".exe")) -AllowedRoot $PortableDir
$ZipPath = Assert-SafeChildPath -Path (Join-Path $ReleaseRoot ($AppName + "-绿色免安装版.zip")) -AllowedRoot $ReleaseRoot
$InstallerScript = Assert-SafeChildPath -Path (Join-Path $ProjectDir "installer\installer.iss") -AllowedRoot $ProjectDir
$InstallerBaseName = $AppName + "-" + $AppVersion + "-Windows-x64-安装版"
$InstallerPath = Assert-SafeChildPath -Path (Join-Path $ReleaseRoot ($InstallerBaseName + ".exe")) -AllowedRoot $ReleaseRoot

Push-Location -LiteralPath $ProjectDir
try {
    Write-Host "[1/10] Checking build prerequisites..."
    Invoke-Python -PythonArguments @(
        "-c",
        "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 'Python 3.11 or newer is required')"
    ) -FailureMessage "Python version check failed"
    $InnoCompilerPath = Find-InnoCompiler
    if (-not (Test-Path -LiteralPath $InstallerScript -PathType Leaf)) {
        throw "Installer script was not found: $InstallerScript"
    }
    $RunningPortableProcesses = @(
        Get-Process -Name $AppName -ErrorAction SilentlyContinue | Where-Object {
            try {
                $_.Path -and [System.IO.Path]::GetFullPath($_.Path).Equals(
                    $PortableExecutable,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            }
            catch {
                $false
            }
        }
    )
    if ($RunningPortableProcesses) {
        $ProcessIds = ($RunningPortableProcesses.Id -join ", ")
        throw "Close the running portable application before building (process ID: $ProcessIds): $PortableExecutable"
    }

    Write-Host "[2/10] Preparing an isolated build environment..."
    if (Test-Path -LiteralPath $BuildEnvRoot) {
        $null = Assert-SafeChildPath -Path $BuildEnvRoot -AllowedRoot $ProjectDir
    }
    & $BootstrapPythonPath -m venv --clear $BuildEnvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Isolated build environment creation failed (exit code $LASTEXITCODE)"
    }
    $PythonPath = Join-Path $BuildEnvRoot "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "The isolated build environment did not create Python: $PythonPath"
    }

    Write-Host "[3/10] Installing locked build dependencies..."
    Invoke-Python -PythonArguments @(
        "-m", "pip", "install", "--disable-pip-version-check",
        "--requirement", (Join-Path $ProjectDir "requirements-build.txt")
    ) -FailureMessage "Dependency installation failed"
    Invoke-Python -PythonArguments @("-m", "pip", "check") -FailureMessage "Dependency verification failed"

    Write-Host "[4/10] Checking Python syntax..."
    $PythonFiles = @(
        "main.py",
        "app_ui.py",
        "calculator_core.py",
        "rate_service.py",
        "settings_service.py",
        "make_icon.py",
        "tests"
    ) | ForEach-Object { Join-Path $ProjectDir $_ }
    Invoke-Python -PythonArguments (@("-B", "-m", "compileall", "-q", "-f") + $PythonFiles) -FailureMessage "Python syntax check failed"

    Write-Host "[5/10] Running automated tests..."
    Invoke-Python -PythonArguments @("-B", "-m", "unittest", "discover", "-s", "tests", "-v") -FailureMessage "Automated tests failed"

    Write-Host "[6/10] Creating application icon..."
    Invoke-Python -PythonArguments @((Join-Path $ProjectDir "make_icon.py")) -FailureMessage "Icon generation failed"

    Write-Host "[7/10] Building portable executable..."
    foreach ($GeneratedPath in @($BuildRoot, $StagedPortableDir)) {
        if (Test-Path -LiteralPath $GeneratedPath) {
            # Assert again immediately before deletion in case the path changed.
            if ($GeneratedPath -eq $BuildRoot) {
                $null = Assert-SafeChildPath -Path $GeneratedPath -AllowedRoot $ProjectDir
            }
            else {
                $null = Assert-SafeChildPath -Path $GeneratedPath -AllowedRoot $DistRoot
            }
            Remove-Item -LiteralPath $GeneratedPath -Recurse -Force
        }
    }
    [System.IO.Directory]::CreateDirectory($BuildRoot) | Out-Null
    [System.IO.Directory]::CreateDirectory($DistRoot) | Out-Null
    Invoke-Python -PythonArguments @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", $DistRoot,
        "--workpath", $BuildRoot,
        (Join-Path $ProjectDir ($AppName + ".spec"))
    ) -FailureMessage "PyInstaller build failed"

    $StagedPortableDir = Assert-SafeChildPath -Path $StagedPortableDir -AllowedRoot $DistRoot
    $StagedExecutable = Join-Path $StagedPortableDir ($AppName + ".exe")
    if (-not (Test-Path -LiteralPath $StagedExecutable -PathType Leaf)) {
        throw "PyInstaller did not create the expected executable: $StagedExecutable"
    }
    Copy-Item -LiteralPath (Join-Path $ProjectDir $GuideName) -Destination (Join-Path $StagedPortableDir $GuideName) -Force
    Copy-Item -LiteralPath (Join-Path $ProjectDir "app.ico") -Destination (Join-Path $StagedPortableDir "app.ico") -Force
    Copy-Item -LiteralPath (Join-Path $ProjectDir "app.png") -Destination (Join-Path $StagedPortableDir "app.png") -Force

    # The distributable must come only from clean staging output. Fail closed if
    # a future packaging change introduces local settings or the runtime cache.
    foreach ($PrivateName in @("app_settings.json", "data")) {
        $PrivatePath = Join-Path $StagedPortableDir $PrivateName
        if (Test-Path -LiteralPath $PrivatePath) {
            throw "Refusing to package private runtime data: $PrivatePath"
        }
    }

    Write-Host "[8/10] Updating local portable copy..."
    $ReleaseRoot = Assert-SafeChildPath -Path $ReleaseRoot -AllowedRoot $ProjectDir
    [System.IO.Directory]::CreateDirectory($ReleaseRoot) | Out-Null
    $PortableDir = Assert-SafeChildPath -Path $PortableDir -AllowedRoot $ReleaseRoot
    [System.IO.Directory]::CreateDirectory($PortableDir) | Out-Null
    $PortableDir = Assert-SafeChildPath -Path $PortableDir -AllowedRoot $ReleaseRoot

    # Replace only known generated items. User settings, cached data and any
    # unrelated files in the portable directory remain untouched. Replacing the
    # entire _internal tree prevents stale bundled libraries from surviving.
    $GeneratedItems = @($AppName + ".exe", "_internal", $GuideName, "app.ico", "app.png")
    foreach ($GeneratedItem in $GeneratedItems) {
        $GeneratedPath = Assert-SafeChildPath -Path (Join-Path $PortableDir $GeneratedItem) -AllowedRoot $PortableDir
        if (Test-Path -LiteralPath $GeneratedPath) {
            Remove-Item -LiteralPath $GeneratedPath -Recurse -Force
        }
    }
    Get-ChildItem -LiteralPath $StagedPortableDir -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $PortableDir -Recurse -Force
    }

    Write-Host "[9/10] Creating privacy-safe portable ZIP..."
    $ZipPath = Assert-SafeChildPath -Path $ZipPath -AllowedRoot $ReleaseRoot
    Compress-Archive -LiteralPath $StagedPortableDir -DestinationPath $ZipPath -CompressionLevel Optimal -Force

    Write-Host "[10/10] Building Windows installer..."
    $InstallerPath = Assert-SafeChildPath -Path $InstallerPath -AllowedRoot $ReleaseRoot
    if (Test-Path -LiteralPath $InstallerPath) {
        Remove-Item -LiteralPath $InstallerPath -Force
    }
    & $InnoCompilerPath "/Qp" "/O$ReleaseRoot" "/F$InstallerBaseName" "/DAppVersion=$AppVersion" $InstallerScript
    if ($LASTEXITCODE -ne 0) {
        throw "Windows installer build failed (exit code $LASTEXITCODE)"
    }
    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
        throw "Inno Setup did not create the expected installer: $InstallerPath"
    }

    Write-Host ""
    Write-Host "Build complete: $(Join-Path $PortableDir ($AppName + '.exe'))" -ForegroundColor Green
    Write-Host "Portable ZIP:  $ZipPath" -ForegroundColor Green
    Write-Host "Installer:     $InstallerPath" -ForegroundColor Green
}
finally {
    Pop-Location
}
