$ErrorActionPreference = "Stop"
$ProjectDir = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$AppName = -join @([char]26332, [char]34913)
$AppVersion = "3.16"
$ReleaseAssetStem = "Yaoheng-{0}-Windows-x64" -f $AppVersion
$GuideName = (-join @([char]20351, [char]29992, [char]35828, [char]26126)) + ".txt"
$ThirdPartyNoticeName = "THIRD-PARTY-NOTICES.txt"
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

if ($env:YAO_HENG_PYTHON) {
    if (-not (Test-Path -LiteralPath $env:YAO_HENG_PYTHON -PathType Leaf)) {
        throw "YAO_HENG_PYTHON does not point to a Python executable: $($env:YAO_HENG_PYTHON)"
    }
    $BootstrapPythonPath = [System.IO.Path]::GetFullPath($env:YAO_HENG_PYTHON)
}
else {
    $PythonCommand = Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $BootstrapPythonPath = $PythonCommand.Source
}
$PythonPath = $BootstrapPythonPath
$PythonLicensePath = Join-Path (Split-Path -Parent $BootstrapPythonPath) "LICENSE.txt"
$DistRoot = Assert-SafeChildPath -Path (Join-Path $ProjectDir "dist") -AllowedRoot $ProjectDir
$BuildRoot = Assert-SafeChildPath -Path (Join-Path $ProjectDir "build") -AllowedRoot $ProjectDir
$BuildEnvRoot = Assert-SafeChildPath -Path (Join-Path $ProjectDir ".venv-build") -AllowedRoot $ProjectDir
$ReleaseRoot = Assert-SafeChildPath -Path (Join-Path $ProjectDir "release") -AllowedRoot $ProjectDir
$StagedPortableDir = Assert-SafeChildPath -Path (Join-Path $DistRoot $AppName) -AllowedRoot $DistRoot
$PortableDir = Assert-SafeChildPath -Path (Join-Path $ReleaseRoot $AppName) -AllowedRoot $ReleaseRoot
$PortableExecutable = Assert-SafeChildPath -Path (Join-Path $PortableDir ($AppName + ".exe")) -AllowedRoot $PortableDir
$PortableAssetName = $ReleaseAssetStem + "-Portable.zip"
$ZipPath = Assert-SafeChildPath -Path (Join-Path $ReleaseRoot $PortableAssetName) -AllowedRoot $ReleaseRoot
$InstallerScript = Assert-SafeChildPath -Path (Join-Path $ProjectDir "installer\installer.iss") -AllowedRoot $ProjectDir
$InstallerBaseName = $ReleaseAssetStem + "-Setup"
$InstallerPath = Assert-SafeChildPath -Path (Join-Path $ReleaseRoot ($InstallerBaseName + ".exe")) -AllowedRoot $ReleaseRoot
$LegacyZipPath = Assert-SafeChildPath -Path (Join-Path $ReleaseRoot ($AppName + "-绿色免安装版.zip")) -AllowedRoot $ReleaseRoot
$LegacyInstallerNamePattern = "^" + [regex]::Escape($AppName) + "-[0-9]+(?:\.[0-9]+){1,3}-Windows-x64-安装版\.exe$"
$ChecksumManifestPath = Assert-SafeChildPath -Path (Join-Path $ReleaseRoot "SHA256SUMS.txt") -AllowedRoot $ReleaseRoot
$ReleaseChecksScript = Assert-SafeChildPath -Path (Join-Path $ProjectDir "tools\release_checks.py") -AllowedRoot $ProjectDir
$PrivacyStrings = @($ProjectDir)
if ($env:USERPROFILE) {
    $PrivacyStrings += [System.IO.Path]::GetFullPath($env:USERPROFILE)
}

Push-Location -LiteralPath $ProjectDir
try {
    Write-Host "[1/12] Checking build prerequisites..."
    if (-not (Test-Path -LiteralPath $ReleaseChecksScript -PathType Leaf)) {
        throw "Release checks script was not found: $ReleaseChecksScript"
    }
    Invoke-Python -PythonArguments @(
        $ReleaseChecksScript, "validate-runtime"
    ) -FailureMessage "Release runtime security check failed"
    $InnoCompilerPath = Find-InnoCompiler
    if (-not (Test-Path -LiteralPath $InstallerScript -PathType Leaf)) {
        throw "Installer script was not found: $InstallerScript"
    }
    if (-not (Test-Path -LiteralPath $PythonLicensePath -PathType Leaf)) {
        throw "Python license file was not found: $PythonLicensePath"
    }
    Invoke-Python -PythonArguments @(
        $ReleaseChecksScript, "validate-asset-names",
        "--asset-name", ([System.IO.Path]::GetFileName($InstallerPath)),
        "--asset-name", ([System.IO.Path]::GetFileName($ZipPath))
    ) -FailureMessage "Release asset name validation failed"
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

    # Remove only the known publishable assets up front. If a later step fails,
    # stale binaries cannot be mistaken for output from the failed build.
    $LegacyPublishablePaths = @($LegacyZipPath)
    if (Test-Path -LiteralPath $ReleaseRoot -PathType Container) {
        $LegacyPublishablePaths += @(
            Get-ChildItem -LiteralPath $ReleaseRoot -File | Where-Object {
                $_.Name -match $LegacyInstallerNamePattern
            } | ForEach-Object { $_.FullName }
        )
    }
    foreach ($PublishablePath in @($ZipPath, $InstallerPath, $ChecksumManifestPath) + $LegacyPublishablePaths) {
        $null = Assert-SafeChildPath -Path $PublishablePath -AllowedRoot $ReleaseRoot
        if (Test-Path -LiteralPath $PublishablePath) {
            Remove-Item -LiteralPath $PublishablePath -Force
        }
    }

    Write-Host "[2/12] Preparing an isolated build environment..."
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

    Write-Host "[3/12] Installing locked build dependencies..."
    Invoke-Python -PythonArguments @(
        "-m", "pip", "install", "--disable-pip-version-check",
        "--no-deps", "--only-binary=:all:",
        "--requirement", (Join-Path $ProjectDir "requirements-build.txt")
    ) -FailureMessage "Dependency installation failed"
    Invoke-Python -PythonArguments @("-m", "pip", "check") -FailureMessage "Dependency verification failed"

    Write-Host "[4/12] Checking Python syntax..."
    $PythonFiles = @(
        "main.py",
        "app_ui.py",
        "calculator_core.py",
        "rate_service.py",
        "settings_service.py",
        "make_icon.py",
        "tools",
        "tests"
    ) | ForEach-Object { Join-Path $ProjectDir $_ }
    Invoke-Python -PythonArguments (@("-B", "-m", "compileall", "-q", "-f") + $PythonFiles) -FailureMessage "Python syntax check failed"

    Write-Host "[5/12] Running automated tests..."
    Invoke-Python -PythonArguments @("-B", "-m", "unittest", "discover", "-s", "tests", "-v") -FailureMessage "Automated tests failed"

    Write-Host "[6/12] Creating application icon..."
    Invoke-Python -PythonArguments @((Join-Path $ProjectDir "make_icon.py")) -FailureMessage "Icon generation failed"

    Write-Host "[7/12] Building portable executable..."
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
    Copy-Item -LiteralPath (Join-Path $ProjectDir "installer\$ThirdPartyNoticeName") -Destination (Join-Path $StagedPortableDir $ThirdPartyNoticeName) -Force

    Write-Host "[8/12] Collecting licenses and auditing staged files..."
    $LicenseArguments = @(
        $ReleaseChecksScript, "collect-licenses",
        "--staging", $StagedPortableDir,
        "--python-license", $PythonLicensePath
    )
    foreach ($Distribution in @(
        "certifi==2026.7.22",
        "charset-normalizer==3.5.1",
        "idna==3.19",
        "requests==2.34.2",
        "tzdata==2026.3",
        "urllib3==2.7.0",
        "pyinstaller==6.22.2"
    )) {
        $LicenseArguments += @("--distribution", $Distribution)
    }
    Invoke-Python -PythonArguments $LicenseArguments -FailureMessage "Third-party license collection failed"
    $StagingAuditArguments = @(
        $ReleaseChecksScript, "verify-staging",
        "--root", $StagedPortableDir,
        "--app-name", $AppName
    )
    foreach ($PrivateString in $PrivacyStrings) {
        $StagingAuditArguments += @("--forbid-string", $PrivateString)
    }
    Invoke-Python -PythonArguments $StagingAuditArguments -FailureMessage "Staged release audit failed"

    Write-Host "[9/12] Updating local portable copy..."
    $ReleaseRoot = Assert-SafeChildPath -Path $ReleaseRoot -AllowedRoot $ProjectDir
    [System.IO.Directory]::CreateDirectory($ReleaseRoot) | Out-Null
    $PortableDir = Assert-SafeChildPath -Path $PortableDir -AllowedRoot $ReleaseRoot
    [System.IO.Directory]::CreateDirectory($PortableDir) | Out-Null
    $PortableDir = Assert-SafeChildPath -Path $PortableDir -AllowedRoot $ReleaseRoot

    # Replace only known generated items. User settings, cached data and any
    # unrelated files in the portable directory remain untouched. Replacing the
    # entire _internal tree prevents stale bundled libraries from surviving.
    $GeneratedItems = @(
        $AppName + ".exe", "_internal", $GuideName, "app.ico", "app.png",
        $ThirdPartyNoticeName, "licenses"
    )
    foreach ($GeneratedItem in $GeneratedItems) {
        $GeneratedPath = Assert-SafeChildPath -Path (Join-Path $PortableDir $GeneratedItem) -AllowedRoot $PortableDir
        if (Test-Path -LiteralPath $GeneratedPath) {
            Remove-Item -LiteralPath $GeneratedPath -Recurse -Force
        }
    }
    Get-ChildItem -LiteralPath $StagedPortableDir -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $PortableDir -Recurse -Force
    }

    Write-Host "[10/12] Creating and verifying the portable ZIP..."
    $ZipPath = Assert-SafeChildPath -Path $ZipPath -AllowedRoot $ReleaseRoot
    Compress-Archive -LiteralPath $StagedPortableDir -DestinationPath $ZipPath -CompressionLevel Optimal -Force
    Invoke-Python -PythonArguments @(
        $ReleaseChecksScript, "verify-zip",
        "--zip", $ZipPath,
        "--staging", $StagedPortableDir,
        "--app-name", $AppName
    ) -FailureMessage "Portable ZIP verification failed"

    Write-Host "[11/12] Building and auditing the Windows installer..."
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
    $InstallerAuditArguments = @(
        $ReleaseChecksScript, "verify-binary",
        "--path", $InstallerPath
    )
    foreach ($PrivateString in $PrivacyStrings) {
        $InstallerAuditArguments += @("--forbid-string", $PrivateString)
    }
    Invoke-Python -PythonArguments $InstallerAuditArguments -FailureMessage "Installer privacy audit failed"

    Write-Host "[12/12] Writing SHA-256 checksums..."
    Invoke-Python -PythonArguments @(
        $ReleaseChecksScript, "checksums",
        "--output", $ChecksumManifestPath,
        "--asset", $InstallerPath,
        "--asset", $ZipPath
    ) -FailureMessage "SHA-256 manifest generation failed"

    Write-Host ""
    Write-Host "Build complete: $(Join-Path $PortableDir ($AppName + '.exe'))" -ForegroundColor Green
    Write-Host "Portable ZIP:  $ZipPath" -ForegroundColor Green
    Write-Host "Installer:     $InstallerPath" -ForegroundColor Green
    Write-Host "SHA-256:       $ChecksumManifestPath" -ForegroundColor Green
}
finally {
    Pop-Location
}
