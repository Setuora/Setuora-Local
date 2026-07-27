@echo off
setlocal
set "SETUORA_UPDATE_BAT=%~f0"
cd /d "%~dp0.."
if /I "%~1"=="--no-pause" (
    shift
)
if "%SETUORA_LAUNCHED_UPDATE%"=="1" (
    rem Give Setuora.exe time to exit so Git can safely replace the launcher itself.
    >nul 2>&1 ping 127.0.0.1 -n 4
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $path=$env:SETUORA_UPDATE_BAT; $marker='### POWERSHELL UPDATE SCRIPT ###'; $raw=Get-Content -Raw -LiteralPath $path; $start=$raw.LastIndexOf($marker); if ($start -lt 0) { throw 'Embedded update script marker not found.' }; $code=$raw.Substring($start + $marker.Length); & ([scriptblock]::Create($code)) @args" %1 %2 %3 %4 %5 %6 %7 %8 %9
set "UPDATE_EXIT=%ERRORLEVEL%"
echo.
if not "%UPDATE_EXIT%"=="0" echo Update did not complete successfully. The error above explains what needs attention.
exit /b %UPDATE_EXIT%

### POWERSHELL UPDATE SCRIPT ###
param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $env:SETUORA_UPDATE_BAT)
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$RequirementsLock = Join-Path $ProjectRoot "requirements.lock"
$StartScript = Join-Path $ProjectRoot "scripts\start_setuora.bat"
$StopScript = Join-Path $ProjectRoot "deployment\windows\stop_setuora.ps1"
$ProcessHelper = Join-Path $ProjectRoot "deployment\windows\server_processes.ps1"
$Caddyfile = Join-Path $ProjectRoot "deployment\caddy\Caddyfile"
$ServiceName = "SetuoraQrTallyBridge"
$CaddyServiceName = "SetuoraCaddy"
$restartAsService = $false
$restartAsConsole = $false
$restartHostAddress = "127.0.0.1"
$restartPort = $Port
$updateStarted = $false

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "== $Title ==" -ForegroundColor Cyan
}

function Test-AdminShell {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Ensure-Pip {
    & $VenvPython -m pip --version | Out-Null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "pip is missing from the virtual environment. Repairing pip with ensurepip..."
    & $VenvPython -m ensurepip --upgrade | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "pip is missing and could not be repaired. Reinstall Python 3.11 with pip enabled, then run scripts\setup.bat again."
    }

    & $VenvPython -m pip --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "pip is still unavailable after repair. Delete .venv, reinstall Python 3.11 with pip enabled, and run scripts\setup.bat again."
    }
}

function Get-CaddyAddress {
    if (-not (Test-Path -LiteralPath $Caddyfile)) {
        return $null
    }
    foreach ($line in Get-Content -LiteralPath $Caddyfile) {
        $trimmed = $line.Trim()
        if ($trimmed -match '^https://([^\s{]+)') {
            return $Matches[1]
        }
    }
    return $null
}

function Wait-HealthEndpoint {
    param([string]$Uri, [string]$DisplayName)

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    $lastError = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                Write-Host "$DisplayName is reachable: $Uri" -ForegroundColor Green
                return
            }
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 1
    }
    throw "$DisplayName did not become reachable at '$Uri'. $lastError"
}

function Start-SetuoraServer {
    if ($restartAsService) {
        & sc.exe config $ServiceName start= auto | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not configure Setuora to start automatically with Windows."
        }
        $svc = Get-Service -Name $ServiceName
        if ($svc.Status -ne "Running") {
            Start-Service -Name $ServiceName
            $svc.WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
        }
        $svc.Refresh()
        if ($svc.Status -ne "Running") {
            throw "Setuora did not remain running after the update. Check logs\setuora-err.log."
        }

        $caddyService = Get-Service -Name $CaddyServiceName -ErrorAction SilentlyContinue
        if ($caddyService) {
            & sc.exe config $CaddyServiceName start= auto depend= $ServiceName | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Could not configure Caddy HTTPS for automatic startup after Setuora."
            }
            if ($caddyService.Status -ne "Running") {
                Start-Service -Name $CaddyServiceName
                $caddyService.WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
            }
            $caddyService.Refresh()
            if ($caddyService.Status -ne "Running") {
                throw "Caddy HTTPS did not remain running after the update. Run Setuora.exe repair."
            }
            Write-Host "Setuora and Caddy HTTPS are running as Windows services." -ForegroundColor Green
        }
        else {
            Write-Host "Setuora is running, but Caddy HTTPS is not installed. Run Setuora.exe setup to enable phone and laptop access." -ForegroundColor Yellow
        }
        Wait-HealthEndpoint -Uri "http://127.0.0.1:$Port/health" -DisplayName "Setuora local health check"
        if ($caddyService) {
            $caddyAddress = Get-CaddyAddress
            if (-not $caddyAddress) {
                throw "Caddy is running, but no HTTPS address could be read from '$Caddyfile'."
            }
            Wait-HealthEndpoint -Uri "https://$caddyAddress/health" -DisplayName "Setuora Caddy HTTPS"
        }
        return $true
    }

    if ($restartAsConsole) {
        Start-Process -FilePath $StartScript -ArgumentList @("-HostAddress", "$restartHostAddress", "-Port", "$restartPort", "--console-only")
        Wait-HealthEndpoint -Uri "http://127.0.0.1:$restartPort/health" -DisplayName "Setuora local health check"
        $caddyAddress = Get-CaddyAddress
        if ($caddyAddress) {
            Wait-HealthEndpoint -Uri "https://$caddyAddress/health" -DisplayName "Setuora Caddy HTTPS"
        }
        Write-Host "Setuora is running in a new window." -ForegroundColor Green
        return $true
    }

    throw "Setuora could not be restarted after the update."
}

function Restore-PreviousVersion {
    if (-not $updateStarted) {
        return
    }
    Write-Host "Rolling back to the previous version ($previousHead)..." -ForegroundColor Yellow
    # This is safe because scripts\update.bat requires a clean worktree before changing
    # source files, and this reset returns to the commit recorded before the update.
    & git reset --hard $previousHead | Out-Host
    Ensure-Pip
    & $VenvPython -m pip install --require-hashes -r $RequirementsLock | Out-Host
}

Set-Location $ProjectRoot

if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    throw "Git was not found. Install Git for Windows, then run scripts\update.bat again."
}
if (-not (Test-Path (Join-Path $ProjectRoot ".git"))) {
    throw "'$ProjectRoot' is not a Git checkout. Clone https://github.com/Dijo-404/Proj_Setu.git before using scripts\update.bat."
}
if (-not (Test-Path $VenvPython)) {
    throw "Setuora is not set up yet. Run scripts\setup.bat first."
}
if (-not (Test-Path $RequirementsLock)) {
    throw "The pinned dependency lockfile is missing: '$RequirementsLock'. Reinstall from a complete release."
}
if (-not (Test-Path $ProcessHelper)) {
    throw "The Setuora process helper is missing: '$ProcessHelper'."
}
. $ProcessHelper

$branch = (@(& git branch --show-current) -join "").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($branch)) {
    throw "The current Git branch could not be determined. Check out the branch you want to update and try again."
}

$originUrl = (@(& git remote get-url origin) -join "").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($originUrl)) {
    throw "The Git remote named 'origin' is missing. It should point to https://github.com/Dijo-404/Proj_Setu.git."
}
if ($originUrl -notmatch "(?i)github\.com[:/]Dijo-404/Proj_Setu(?:\.git)?/?$") {
    throw "The Git remote named 'origin' points to '$originUrl', not https://github.com/Dijo-404/Proj_Setu.git."
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$caddyService = Get-Service -Name $CaddyServiceName -ErrorAction SilentlyContinue
$restartAsService = [bool]$service
$runningSetuoraProcesses = @(Get-SetuoraServerProcesses -ProjectRoot $ProjectRoot -ExcludeProcessIds @($PID))
$restartAsConsole = -not $restartAsService
if ($runningSetuoraProcesses.Count -gt 0) {
    $launchInfo = Get-SetuoraServerLaunchInfo -Process $runningSetuoraProcesses[0] -DefaultHostAddress $restartHostAddress -DefaultPort $restartPort
    $restartHostAddress = $launchInfo.HostAddress
    $restartPort = $launchInfo.Port
}
if (($service -or $caddyService) -and -not (Test-AdminShell)) {
    throw "Setuora or Caddy is installed as a Windows service. Right-click scripts\update.bat, choose 'Run as administrator', and try again."
}

$worktreeChanges = @(& git status --porcelain)
if ($LASTEXITCODE -ne 0) {
    throw "Git could not inspect the working tree. Your installation was left unchanged."
}
if ($worktreeChanges.Count -gt 0) {
    throw "Refusing to update because local source changes are present. Commit or stash them first; scripts\update.bat never overwrites local code."
}

# Remember the current commit so a failed update can roll back.
$previousHead = (@(& git rev-parse HEAD) -join "").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($previousHead)) {
    throw "The current commit could not be determined. Run scripts\update.bat again."
}

Write-Section "Download Latest Version"
Write-Host "Updating branch '$branch' from origin..."
& git fetch --no-tags origin $branch
if ($LASTEXITCODE -ne 0) {
    throw "Git could not download the latest version. Your existing files were left intact; check the Git message above and run scripts\update.bat again."
}

$fetchedHead = (@(& git rev-parse FETCH_HEAD) -join "").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($fetchedHead)) {
    throw "The downloaded version could not be verified. Your existing files were left intact."
}
if ($fetchedHead -eq $previousHead) {
    Write-Host "Setuora source is already up to date." -ForegroundColor Green
    Write-Section "Ensure Services Are Running"
    Start-SetuoraServer | Out-Null
    Write-Host "Setuora and its available HTTPS services are running." -ForegroundColor Green
    return
}

# Prefer a normal fast-forward. If release history was rewritten, preserve the
# clean installed commit on a backup branch before realigning source to the
# verified origin. Ignored runtime data and settings are not touched by Git.
& git merge-base --is-ancestor $previousHead $fetchedHead
$ancestryResult = $LASTEXITCODE
if ($ancestryResult -eq 0) {
    & git merge --ff-only FETCH_HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "The downloaded fast-forward could not be applied. Your existing files were left unchanged."
    }
}
elseif ($ancestryResult -eq 1) {
    $shortHead = (@(& git rev-parse --short $previousHead) -join "").Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($shortHead)) {
        throw "The current version could not be named for safe backup. Your existing files were left unchanged."
    }
    $backupBranch = "setuora-backup/$(Get-Date -Format 'yyyyMMdd-HHmmssfff')-$shortHead"
    Write-Host "Release history differs from this installation." -ForegroundColor Yellow
    Write-Host "Preserving the installed commit as '$backupBranch' before updating..." -ForegroundColor Yellow
    & git branch $backupBranch $previousHead
    if ($LASTEXITCODE -ne 0) {
        throw "The installed commit could not be preserved on a backup branch. Your existing files were left unchanged."
    }
    & git reset --hard FETCH_HEAD
    if ($LASTEXITCODE -ne 0) {
        throw "The downloaded release could not be applied. The previous commit remains available as '$backupBranch'."
    }
}
else {
    throw "Git could not compare the installed and downloaded histories. Your existing files were left unchanged."
}
$updateStarted = $true

if (-not (Test-Path $StopScript)) {
    throw "The server management helper is missing after the update: '$StopScript'."
}

Write-Section "Stop Existing Server"
& $StopScript -ProjectDir $ProjectRoot -ServiceName $ServiceName

# Stop before pip: Windows can't replace .pyd/.dll files a running server holds.
# On failure the server is already down, so roll back and restart it.
try {
    Write-Section "Update Dependencies"
    Ensure-Pip
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upgrade pip."
    }

    & $VenvPython -m pip install --require-hashes -r $RequirementsLock
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed."
    }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Installed Python dependencies are inconsistent. Check the pip message above."
    }

    Write-Section "Smoke Test"
    & $VenvPython -c "import uvicorn; from app.main import app; print('App import OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "The updated app could not be imported. Check the error above."
    }

    Write-Section "Regression Tests"
    & $VenvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        throw "The updated release did not pass its test suite."
    }
}
catch {
    Write-Section "Recover After Failed Update"
    Write-Host "The update failed after the server was stopped: $($_.Exception.Message)" -ForegroundColor Red
    try {
        Restore-PreviousVersion
    }
    catch {
        Write-Host "Automatic rollback failed. Resolve the Git/pip message above before retrying." -ForegroundColor Red
    }
    try {
        $serverRestarted = Start-SetuoraServer
        if ($serverRestarted) {
            Write-Host "The previous version was restored and the server is running again." -ForegroundColor Yellow
        }
        else {
            Write-Host "The previous version was restored. Setuora was left stopped because it was not running before the update." -ForegroundColor Yellow
        }
    }
    catch {
        Write-Host "The server could not be restarted automatically. Start it manually with scripts\start_setuora.bat." -ForegroundColor Red
    }
    throw
}

Write-Section "Restart Server"
$serverRestarted = Start-SetuoraServer
Write-Host "Setuora was updated successfully." -ForegroundColor Green
if ($serverRestarted) {
    if ($restartAsService) {
        Write-Host "Local URL: http://127.0.0.1:$Port"
    }
    else {
        Write-Host "Local URL: http://${restartHostAddress}:$restartPort"
    }
}
