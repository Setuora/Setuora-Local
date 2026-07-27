param(
    [Parameter(Mandatory=$true)][string]$ProjectDir,
    [Parameter(Mandatory=$true)][string]$NssmPath,
    [string]$ServiceName = "SetuoraQrTallyBridge",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$pythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$logDir = Join-Path $ProjectDir "logs"
$dataDir = Join-Path $ProjectDir "data"
$localServiceSid = "*S-1-5-19"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Virtual-environment Python was not found at '$pythonExe'."
}
if (-not (Test-Path -LiteralPath $NssmPath)) {
    throw "NSSM was not found at '$NssmPath'."
}

function Invoke-Nssm {
    & $NssmPath @args | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "NSSM command failed: nssm $($args -join ' ')"
    }
}

function Grant-LocalServiceAccess {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Access
    )

    & icacls.exe $Path /grant "${localServiceSid}:(OI)(CI)$Access" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not grant LocalService $Access access to '$Path'."
    }
}

function Get-VenvBasePythonHome {
    param([Parameter(Mandatory=$true)][string]$VenvDir)

    $configPath = Join-Path $VenvDir "pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $configPath)) {
        return $null
    }
    foreach ($line in Get-Content -LiteralPath $configPath) {
        if ($line -match '^\s*home\s*=\s*(.+?)\s*$') {
            return $Matches[1].Trim()
        }
    }
    return $null
}

# The service can read application code but only write its database and logs.
Grant-LocalServiceAccess -Path $ProjectDir -Access "RX"
Grant-LocalServiceAccess -Path $dataDir -Access "M"
Grant-LocalServiceAccess -Path $logDir -Access "M"

# A per-user (winget) base Python lives in a profile LocalService cannot read,
# which makes the venv stub fail with "No Python at '...'". Grant it access.
$basePythonHome = Get-VenvBasePythonHome -VenvDir (Join-Path $ProjectDir ".venv")
if ($basePythonHome) {
    $basePythonHome = [IO.Path]::GetFullPath($basePythonHome).TrimEnd("\")
    $projectFull = [IO.Path]::GetFullPath($ProjectDir).TrimEnd("\")
    $insideProject = $basePythonHome.StartsWith(
        $projectFull + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )
    if ((Test-Path -LiteralPath $basePythonHome) -and -not $insideProject) {
        Write-Host "Granting the service account access to the base Python at '$basePythonHome'..."
        Grant-LocalServiceAccess -Path $basePythonHome -Access "RX"
    }
}

$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $existingService) {
    Invoke-Nssm install $ServiceName $pythonExe
}
elseif ($existingService.Status -ne "Stopped") {
    Invoke-Nssm stop $ServiceName
}

Invoke-Nssm set $ServiceName Application $pythonExe
Invoke-Nssm set $ServiceName AppParameters "-m uvicorn app.main:app --host 127.0.0.1 --port $Port"
Invoke-Nssm set $ServiceName AppDirectory $ProjectDir
Invoke-Nssm set $ServiceName AppStdout (Join-Path $logDir "setuora-out.log")
Invoke-Nssm set $ServiceName AppStderr (Join-Path $logDir "setuora-err.log")
Invoke-Nssm set $ServiceName AppRotateFiles 1
Invoke-Nssm set $ServiceName AppRotateBytes 10485760
Invoke-Nssm set $ServiceName Start SERVICE_AUTO_START
Invoke-Nssm set $ServiceName AppExit Default Restart
Invoke-Nssm set $ServiceName AppThrottle 15000
Invoke-Nssm set $ServiceName AppRestartDelay 5000
# Never run the web application as LocalSystem. LocalService has no administrator
# privileges and only has write access to the runtime directories above.
Invoke-Nssm set $ServiceName ObjectName "NT AUTHORITY\LocalService" ""
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not configure automatic recovery for the Setuora Windows service."
}
& sc.exe failureflag $ServiceName 1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not enable failure recovery for the Setuora Windows service."
}
