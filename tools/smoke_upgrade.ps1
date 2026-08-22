param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^\d{1,4}(\.\d{1,4}){1,3}$")]
    [string]$PreviousVersion
)

$ErrorActionPreference = "Stop"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$VersionSource = Get-Content -LiteralPath (Join-Path $ProjectRoot "app_version.py") -Raw
$VersionMatch = [regex]::Match($VersionSource, '(?m)^APP_VERSION\s*=\s*"(?<version>\d{1,4}(?:\.\d{1,4}){1,3})"$')
if (-not $VersionMatch.Success) {
    throw "Unable to read APP_VERSION from app_version.py"
}
$CurrentVersion = $VersionMatch.Groups["version"].Value
if ($PreviousVersion -eq $CurrentVersion) {
    throw "PreviousVersion must differ from current version $CurrentVersion"
}
$TempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar
)
$RequiredPrefix = $TempRoot + [System.IO.Path]::DirectorySeparatorChar
$SmokeRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $TempRoot ("Yaoheng-Upgrade-Smoke-" + [guid]::NewGuid().ToString("N")))
)
if (-not $SmokeRoot.StartsWith($RequiredPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing an unsafe smoke-test path: $SmokeRoot"
}

$ArtifactDir = Join-Path $SmokeRoot "artifacts"
$InstallDir = Join-Path $SmokeRoot "installed"
[System.IO.Directory]::CreateDirectory($ArtifactDir) | Out-Null
[System.IO.Directory]::CreateDirectory($InstallDir) | Out-Null

$InnoCompiler = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) {
    throw "Inno Setup 6 was not found: $InnoCompiler"
}
$InstallerScript = Join-Path $ProjectRoot "installer\installer.iss"
$AppProcess = $null

function Invoke-CheckedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    $Process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -Wait `
        -PassThru `
        -WindowStyle Hidden
    if ($Process.ExitCode -ne 0) {
        throw "$FailureMessage (exit code $($Process.ExitCode))"
    }
}

try {
    & $InnoCompiler "/Qp" "/O$ArtifactDir" "/FYaoheng-Upgrade-Smoke-$PreviousVersion" "/DAppVersion=$PreviousVersion" "/DUpgradeSmokeTest=1" $InstallerScript
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to compile the old smoke installer"
    }
    & $InnoCompiler "/Qp" "/O$ArtifactDir" "/FYaoheng-Upgrade-Smoke-$CurrentVersion" "/DAppVersion=$CurrentVersion" "/DUpgradeSmokeTest=1" $InstallerScript
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to compile the new smoke installer"
    }

    $OldSetup = Join-Path $ArtifactDir "Yaoheng-Upgrade-Smoke-$PreviousVersion.exe"
    $NewSetup = Join-Path $ArtifactDir "Yaoheng-Upgrade-Smoke-$CurrentVersion.exe"
    $InstallArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CURRENTUSER",
        ("/DIR=" + $InstallDir)
    )
    Invoke-CheckedProcess $OldSetup $InstallArguments "Old smoke installation failed"

    [System.IO.Directory]::CreateDirectory((Join-Path $InstallDir "private")) | Out-Null
    [System.IO.Directory]::CreateDirectory((Join-Path $InstallDir "data")) | Out-Null
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination (Join-Path $InstallDir "app_settings.json")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination (Join-Path $InstallDir "private\sentinel.keep")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination (Join-Path $InstallDir "data\sentinel.keep")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") -Destination (Join-Path $InstallDir "_internal\obsolete-v3201.bin")
    $SettingsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $InstallDir "app_settings.json")).Hash

    Invoke-CheckedProcess $NewSetup $InstallArguments "Upgrade smoke installation failed"
    if (Test-Path -LiteralPath (Join-Path $InstallDir "_internal\obsolete-v3201.bin")) {
        throw "An obsolete runtime file survived the upgrade"
    }
    foreach ($RelativePath in @("app_settings.json", "private\sentinel.keep", "data\sentinel.keep")) {
        if (-not (Test-Path -LiteralPath (Join-Path $InstallDir $RelativePath) -PathType Leaf)) {
            throw "A user file was not preserved: $RelativePath"
        }
    }
    $InstalledSettingsHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $InstallDir "app_settings.json")
    ).Hash
    if ($InstalledSettingsHash -ne $SettingsHash) {
        throw "Settings changed during the upgrade"
    }
    $GuideHeading = Get-Content -LiteralPath (Join-Path $InstallDir "使用说明.txt") -TotalCount 1
    if ($GuideHeading -notmatch [regex]::Escape($CurrentVersion)) {
        throw "The installed guide does not report version $CurrentVersion"
    }

    Copy-Item `
        -LiteralPath (Join-Path $ProjectRoot "app_settings.example.json") `
        -Destination (Join-Path $InstallDir "app_settings.json") `
        -Force
    $InstalledExecutable = Join-Path $InstallDir "曜衡.exe"
    $AppProcess = Start-Process `
        -FilePath $InstalledExecutable `
        -WorkingDirectory $InstallDir `
        -PassThru `
        -WindowStyle Hidden
    Start-Sleep -Seconds 4
    if ($AppProcess.HasExited) {
        throw "The primary packaged app exited early (exit code $($AppProcess.ExitCode))"
    }

    $SecondProcess = Start-Process `
        -FilePath $InstalledExecutable `
        -WorkingDirectory $InstallDir `
        -PassThru `
        -WindowStyle Hidden
    if (-not $SecondProcess.WaitForExit(8000)) {
        Stop-Process -Id $SecondProcess.Id -Force
        throw "The second packaged app did not exit"
    }
    if ($SecondProcess.ExitCode -ne 0) {
        throw "The second packaged app returned exit code $($SecondProcess.ExitCode)"
    }
    if ($AppProcess.HasExited) {
        throw "The primary packaged app exited after the repeated launch"
    }

    Write-Output "UPGRADE_SMOKE=PASS"
    Write-Output "SINGLE_INSTANCE_PACKAGED_SMOKE=PASS"
}
finally {
    if ($AppProcess -and -not $AppProcess.HasExited) {
        Stop-Process -Id $AppProcess.Id -Force -ErrorAction SilentlyContinue
        $AppProcess.WaitForExit(5000) | Out-Null
    }
    $Uninstaller = Join-Path $InstallDir "unins000.exe"
    if (Test-Path -LiteralPath $Uninstaller -PathType Leaf) {
        $UninstallProcess = Start-Process `
            -FilePath $Uninstaller `
            -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
            -Wait `
            -PassThru `
            -WindowStyle Hidden
    }
    if (Test-Path -LiteralPath $SmokeRoot) {
        $ResolvedSmokeRoot = [System.IO.Path]::GetFullPath($SmokeRoot)
        if (-not $ResolvedSmokeRoot.StartsWith($RequiredPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing unsafe smoke-test cleanup: $ResolvedSmokeRoot"
        }
        [System.IO.Directory]::Delete($ResolvedSmokeRoot, $true)
    }
}
