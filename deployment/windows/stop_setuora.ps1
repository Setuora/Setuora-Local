param(
    [Parameter(Mandatory=$true)][string]$ProjectDir,
    [string]$ServiceName = "SetuoraQrTallyBridge",
    [string]$CaddyServiceName = "SetuoraCaddy"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = [IO.Path]::GetFullPath($ProjectDir).TrimEnd("\")
$startScript = [IO.Path]::GetFullPath((Join-Path $projectRoot "scripts\start_setuora.bat"))
$startHelper = [IO.Path]::GetFullPath((Join-Path $projectRoot "deployment\windows\start_setuora.ps1"))
$processHelper = Join-Path $PSScriptRoot "server_processes.ps1"
$stoppedAnything = $false

if (-not (Test-Path -LiteralPath $processHelper)) {
    throw "The Setuora process helper was not found: '$processHelper'."
}
. $processHelper

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne "Stopped") {
    Write-Host "Stopping the Setuora Windows service..."
    try {
        Stop-Service -Name $ServiceName -Force
        $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
    }
    catch [System.ServiceProcess.TimeoutException] {
        throw "The Setuora Windows service did not stop within 20 seconds. Check the service in Windows Services, then try again."
    }
    catch {
        throw "Windows could not stop the Setuora service. Run this script as Administrator. $($_.Exception.Message)"
    }
    $stoppedAnything = $true
}

$setuoraProcesses = @(Get-SetuoraServerProcesses -ProjectRoot $projectRoot -ExcludeProcessIds @($PID))

foreach ($process in $setuoraProcesses) {
    $launcher = Get-SetuoraLauncherProcess -ServerProcess $process -StartScript $startScript -StartHelper $startHelper

    if ($launcher) {
        Write-Host "Stopping the existing Setuora server window..."
        & taskkill.exe /PID $launcher.ProcessId /T /F | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Windows could not stop the existing Setuora server window."
        }
    }
    elseif (Get-Process -Id $process.ProcessId -ErrorAction SilentlyContinue) {
        Write-Host "Stopping the existing Setuora server process..."
        Stop-Process -Id $process.ProcessId -Force
    }
    $stoppedAnything = $true
}

$caddyService = Get-Service -Name $CaddyServiceName -ErrorAction SilentlyContinue
if ($caddyService -and $caddyService.Status -ne "Stopped") {
    Write-Host "Stopping the Setuora HTTPS proxy..."
    try {
        Stop-Service -Name $CaddyServiceName -Force
        $caddyService.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
    }
    catch [System.ServiceProcess.TimeoutException] {
        throw "The Setuora HTTPS proxy did not stop within 20 seconds. Check the Caddy service, then try again."
    }
    catch {
        throw "Windows could not stop the Setuora HTTPS proxy. Run this command as Administrator. $($_.Exception.Message)"
    }
    $stoppedAnything = $true
}

if (-not $stoppedAnything) {
    Write-Host "No existing Setuora server process was found."
}
