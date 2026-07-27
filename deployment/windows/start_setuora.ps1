param(
    [Parameter(Mandatory=$true)][string]$ProjectDir,
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [string]$ServiceName = "SetuoraQrTallyBridge",
    [string]$CaddyServiceName = "SetuoraCaddy",
    [switch]$ConsoleOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = [IO.Path]::GetFullPath($ProjectDir).TrimEnd("\")
$pythonExe = [IO.Path]::GetFullPath((Join-Path $projectRoot ".venv\Scripts\python.exe"))
$requirementsPath = Join-Path $projectRoot "requirements.lock"
$processHelper = Join-Path $PSScriptRoot "server_processes.ps1"
$caddyfile = Join-Path $projectRoot "deployment\caddy\Caddyfile"
Set-Location $projectRoot

function Ensure-Pip {
    param([string]$PythonExe)

    & $PythonExe -m pip --version | Out-Null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "pip is missing from the virtual environment. Repairing pip with ensurepip..."
    & $PythonExe -m ensurepip --upgrade | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "pip is missing and could not be repaired. Run scripts\setup.bat again after reinstalling Python 3.11 with pip enabled."
    }

    & $PythonExe -m pip --version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "pip is still unavailable after repair. Delete .venv, reinstall Python 3.11 with pip enabled, and run scripts\setup.bat again."
    }
}

function Test-AppDependencies {
    param(
        [string]$PythonExe,
        [switch]$Quiet
    )

    if ($Quiet) {
        & $PythonExe -c "import uvicorn; from app.main import app" | Out-Null
    }
    else {
        & $PythonExe -c "import uvicorn; from app.main import app"
    }
    return ($LASTEXITCODE -eq 0)
}

function Ensure-AppDependencies {
    param(
        [string]$PythonExe,
        [string]$RequirementsPath
    )

    if (Test-AppDependencies -PythonExe $PythonExe -Quiet) {
        return
    }

    if (-not (Test-Path -LiteralPath $RequirementsPath)) {
        throw "Python packages are missing and requirements.lock was not found at '$RequirementsPath'. Reinstall from a complete release."
    }

    Write-Host "Python packages are missing or incomplete. Installing requirements..."
    Ensure-Pip -PythonExe $PythonExe

    & $PythonExe -m pip install --upgrade pip | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upgrade pip."
    }

    & $PythonExe -m pip install --require-hashes -r $RequirementsPath | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed. Check the pip message above, then run scripts\setup.bat again."
    }

    & $PythonExe -m pip check | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Installed Python dependencies are inconsistent. Check the pip message above."
    }

    if (-not (Test-AppDependencies -PythonExe $PythonExe)) {
        throw "The app still could not be imported after installing requirements. Check the Python error above."
    }
}

function Start-CaddyProxy {
    $caddyService = Get-Service -Name $CaddyServiceName -ErrorAction SilentlyContinue
    if (-not $caddyService) {
        Write-Host "Caddy HTTPS is not installed. Run Setuora.exe setup to enable access from phones and laptops." -ForegroundColor Yellow
        return $false
    }
    $appService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($appService) {
        & sc.exe config $CaddyServiceName start= auto depend= $ServiceName | Out-Null
    }
    else {
        & sc.exe config $CaddyServiceName start= auto | Out-Null
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Caddy HTTPS could not be configured for Windows autostart. Run Setuora.exe start as Administrator."
    }
    if ($caddyService.Status -ne "Running") {
        Write-Host "Starting the Setuora Caddy HTTPS proxy..."
        try {
            Start-Service -Name $CaddyServiceName
            $caddyService.WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
        }
        catch {
            throw "Caddy HTTPS could not start. Run Setuora.exe repair as Administrator, then check Windows Event Viewer if the error continues. $($_.Exception.Message)"
        }
    }
    $caddyService.Refresh()
    if ($caddyService.Status -ne "Running") {
        throw "Caddy HTTPS started and then stopped. Run Setuora.exe repair and validate deployment\caddy\Caddyfile."
    }
    Write-Host "Setuora Caddy HTTPS proxy is running." -ForegroundColor Green
    return $true
}

function Get-CaddyAddress {
    if (-not (Test-Path -LiteralPath $caddyfile)) {
        return $null
    }
    foreach ($line in Get-Content -LiteralPath $caddyfile) {
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

if (-not (Test-Path -LiteralPath $processHelper)) {
    throw "The Setuora process helper was not found: '$processHelper'."
}
. $processHelper

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Setuora is not set up yet. Run scripts\setup.bat first."
}

Ensure-AppDependencies -PythonExe $pythonExe -RequirementsPath $requirementsPath

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service -and -not $ConsoleOnly) {
    & sc.exe config $ServiceName start= auto | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Setuora could not be configured for Windows autostart. Run Setuora.exe start as Administrator."
    }
    if ($service.Status -eq "Running") {
        Write-Host "Setuora is already running as the Windows service."
    }
    else {
        Write-Host "Starting the Setuora Windows service..."
        try {
            Start-Service -Name $ServiceName
            $service.WaitForStatus("Running", [TimeSpan]::FromSeconds(20))
            Write-Host "Setuora is running as the Windows service." -ForegroundColor Green
        }
        catch [System.ServiceProcess.TimeoutException] {
            throw "The Setuora Windows service did not start within 20 seconds. Check Windows Services, then try again."
        }
        catch {
            throw "Windows could not start the Setuora service. Run scripts\start_setuora.bat as Administrator. $($_.Exception.Message)"
        }
    }

    $caddyRunning = Start-CaddyProxy
    Wait-HealthEndpoint -Uri "http://127.0.0.1:$Port/health" -DisplayName "Setuora local health check"
    if ($caddyRunning) {
        $caddyAddress = Get-CaddyAddress
        if (-not $caddyAddress) {
            throw "Caddy is running, but no HTTPS address could be read from '$caddyfile'."
        }
        Wait-HealthEndpoint -Uri "https://$caddyAddress/health" -DisplayName "Setuora Caddy HTTPS"
    }
    Write-Host "Use scripts\stop_setuora.bat to stop it."
    return
}

Start-CaddyProxy | Out-Null
$serverProcesses = @(Get-SetuoraServerProcesses -ProjectRoot $projectRoot -ExcludeProcessIds @($PID))
if ($serverProcesses.Count -gt 0) {
    $processIds = ($serverProcesses | ForEach-Object { $_.ProcessId }) -join ", "
    Write-Host "Setuora is already running in another window or background process. PID(s): $processIds"
    Write-Host "Use scripts\stop_setuora.bat to stop it before starting a fresh server."
    return
}

Write-Host "Starting Setuora QR Tally Bridge..."
Write-Host "Open: http://${HostAddress}:$Port"
Write-Host "Press Ctrl+C in this window to stop the app."
& $pythonExe -m uvicorn app.main:app --host $HostAddress --port $Port
exit $LASTEXITCODE
