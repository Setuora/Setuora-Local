@echo off
setlocal
set "SETUORA_SETUP_BAT=%~f0"
cd /d "%~dp0.."
if /I "%~1"=="--no-pause" (
    shift
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $path=$env:SETUORA_SETUP_BAT; $marker='### POWERSHELL SETUP SCRIPT ###'; $raw=Get-Content -Raw -LiteralPath $path; $start=$raw.LastIndexOf($marker); if ($start -lt 0) { throw 'Embedded setup script marker not found.' }; $code=$raw.Substring($start + $marker.Length); & ([scriptblock]::Create($code)) @args" %1 %2 %3 %4 %5 %6 %7 %8 %9
set "SETUP_EXIT=%ERRORLEVEL%"
echo.
if not "%SETUP_EXIT%"=="0" echo Setup did not complete successfully.
exit /b %SETUP_EXIT%

### POWERSHELL SETUP SCRIPT ###
param(
    [int]$Port = 8000,
    [switch]$SkipStart,
    [switch]$SkipCaddy,
    [switch]$ConfigureCaddy,
    [switch]$Repair
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $env:SETUORA_SETUP_BAT)
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$RequirementsLock = Join-Path $ProjectRoot "requirements.lock"
$StartScript = Join-Path $ProjectRoot "scripts\start_setuora.bat"
$StopScript = Join-Path $ProjectRoot "deployment\windows\stop_setuora.ps1"
$ProcessHelper = Join-Path $ProjectRoot "deployment\windows\server_processes.ps1"
$EnvPath = Join-Path $ProjectRoot ".env"
$DataDir = Join-Path $ProjectRoot "data"
$LogsDir = Join-Path $ProjectRoot "logs"
$CaddyDir = Join-Path $ProjectRoot "deployment\caddy"
$Caddyfile = Join-Path $CaddyDir "Caddyfile"
$CaddyServiceName = "SetuoraCaddy"
$AppServiceName = "SetuoraQrTallyBridge"
$CaddyServiceStartName = "NT AUTHORITY\LocalService"
$LegacyCaddyServiceNames = @("SetuCaddy")
$LocalServiceSid = "*S-1-5-19"
$restartAsService = $false
$restartAsConsole = $false
$restartHostAddress = "127.0.0.1"
$restartPort = $Port

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "== $Title ==" -ForegroundColor Cyan
}

function Read-Default {
    param(
        [string]$Prompt,
        [string]$Default
    )

    $answer = Read-Host "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($answer)) {
        return $Default
    }
    return $answer.Trim()
}

function Read-YesNo {
    param(
        [string]$Prompt,
        [bool]$Default = $true
    )

    $suffix = if ($Default) { "Y/n" } else { "y/N" }
    while ($true) {
        $answer = Read-Host "$Prompt [$suffix]"
        if ([string]::IsNullOrWhiteSpace($answer)) {
            return $Default
        }

        switch ($answer.Trim().ToLowerInvariant()) {
            "y" { return $true }
            "yes" { return $true }
            "n" { return $false }
            "no" { return $false }
            default { Write-Host "Please answer yes or no." -ForegroundColor Yellow }
        }
    }
}

function ConvertFrom-SecureText {
    param([System.Security.SecureString]$Secure)

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function New-RandomSecret {
    param([int]$ByteCount = 32)

    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }

    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Read-Password {
    param([string]$Username)

    while ($true) {
        Write-Host "Choose a password for the first admin user '$Username'."
        Write-Host "Leave it blank to let this setup generate one for you."
        $first = ConvertFrom-SecureText (Read-Host "Admin password" -AsSecureString)
        if ([string]::IsNullOrWhiteSpace($first)) {
            return New-RandomSecret -ByteCount 14
        }

        $second = ConvertFrom-SecureText (Read-Host "Confirm admin password" -AsSecureString)
        if ($first -eq $second) {
            if ($first.Length -lt 8) {
                $useShort = Read-YesNo "That password is short. Use it anyway?" $false
                if (-not $useShort) {
                    continue
                }
            }
            return $first
        }

        Write-Host "Passwords did not match. Please try again." -ForegroundColor Yellow
    }
}

function Get-PythonCommand {
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "py"; Args = @("-3") },
        @{ Exe = "python"; Args = @() },
        @{ Exe = "python3"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        $versionArgs = @($candidate.Args) + @("-c", "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)")
        try {
            & $candidate.Exe @versionArgs | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return $candidate
            }
        }
        catch {
            continue
        }
    }

    return $null
}

function Install-PythonWithWinget {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        return $false
    }

    Write-Host "Python 3.11 was not found. Trying to install it using winget..."
    & winget install --id Python.Python.3.11 -e --source winget --accept-package-agreements --accept-source-agreements
    return ($LASTEXITCODE -eq 0)
}

function Ensure-Python {
    $python = Get-PythonCommand
    if ($python) {
        return $python
    }

    $install = Read-YesNo "Python 3.11 is required. Install it now with winget?" $true
    if ($install -and (Install-PythonWithWinget)) {
        $python = Get-PythonCommand
        if ($python) {
            return $python
        }
    }

    throw "Python 3.11 was not found. Install it from https://www.python.org/downloads/ and run setup again."
}

function Ensure-Venv {
    param($Python)

    if (Test-Path $VenvPython) {
        $venvHealthy = $false
        try {
            & $VenvPython -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" | Out-Null
            $venvHealthy = ($LASTEXITCODE -eq 0)
        }
        catch {
            $venvHealthy = $false
        }

        if ($venvHealthy) {
            Write-Host "Virtual environment is healthy."
            return
        }

        Write-Host "The virtual environment is damaged or incompatible. Rebuilding it..." -ForegroundColor Yellow
        Remove-Item -LiteralPath $VenvDir -Recurse -Force
    }

    Write-Host "Creating Python virtual environment..."
    $args = @($Python.Args) + @("-m", "venv", $VenvDir)
    & $Python.Exe @args
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the virtual environment."
    }
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

function Install-Dependencies {
    Ensure-Pip

    if (-not (Test-Path $RequirementsLock)) {
        throw "The pinned dependency lockfile is missing: '$RequirementsLock'. Reinstall from a complete release."
    }

    Write-Host "Installing Python packages. This can take a few minutes..."
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upgrade pip."
    }

    & $VenvPython -m pip install --require-hashes -r $RequirementsLock
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install project dependencies."
    }

    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Installed Python dependencies are inconsistent. Check the pip message above."
    }
}

function Write-EnvFile {
    $existingDatabase = Test-Path (Join-Path $DataDir "setuora.db")
    if ($existingDatabase) {
        Write-Host "Existing data\setuora.db found. Changing bootstrap admin details will not change existing users." -ForegroundColor Yellow
    }

    $appName = Read-Default "App display name" "Setuora QR Tally Bridge"
    $adminUser = Read-Default "First admin username" "admin"
    $adminPassword = Read-Password $adminUser
    $sessionTimeout = Read-Default "Login session timeout in minutes" "480"
    $secureCookie = Read-YesNo "Will this app be opened only through HTTPS right now?" $false
    $databaseUrl = "sqlite:///./data/setuora.db"
    $secret = New-RandomSecret

    $secureCookieText = if ($secureCookie) { "true" } else { "false" }
    $lines = @(
        "APP_NAME=$appName",
        "APP_SECRET_KEY=$secret",
        "DATABASE_URL=$databaseUrl",
        "SESSION_TIMEOUT_MINUTES=$sessionTimeout",
        "BOOTSTRAP_ADMIN_USERNAME=$adminUser",
        "BOOTSTRAP_ADMIN_PASSWORD=$adminPassword",
        "SESSION_COOKIE_SECURE=$secureCookieText",
        "LOGIN_MAX_ATTEMPTS=8",
        "LOGIN_LOCKOUT_MINUTES=15"
    )

    Set-Content -Path $EnvPath -Value $lines -Encoding UTF8

    return @{
        Username = $adminUser
        Password = $adminPassword
        SecureCookie = $secureCookieText
    }
}

function Test-EnvFileHasSafeBootstrapPassword {
    if (-not (Test-Path $EnvPath)) {
        return $false
    }

    $line = Get-Content -LiteralPath $EnvPath |
        Where-Object { $_ -match "^BOOTSTRAP_ADMIN_PASSWORD=" } |
        Select-Object -First 1
    if (-not $line) {
        return $false
    }

    $password = $line.Substring("BOOTSTRAP_ADMIN_PASSWORD=".Length)
    return (
        $password.Length -ge 8 -and
        $password -notin @("admin123", "change-this-password", "change-this-before-first-start")
    )
}

function Ensure-EnvFile {
    if (Test-Path $EnvPath) {
        $safeBootstrap = Test-EnvFileHasSafeBootstrapPassword
        if ($safeBootstrap) {
            if (Read-YesNo ".env already exists. Keep it as-is?" $true) {
                return $null
            }
        }
        else {
            if (-not (Read-YesNo ".env has no safe first-admin password. Replace it now?" $true)) {
                return $null
            }
        }
    }

    return Write-EnvFile
}

function Set-EnvSetting {
    param(
        [string]$Name,
        [string]$Value
    )

    if (-not (Test-Path $EnvPath)) {
        return
    }

    $pattern = "^" + [Regex]::Escape($Name) + "="
    $found = $false
    $lines = @(
        foreach ($line in (Get-Content -LiteralPath $EnvPath)) {
            if ($line -match $pattern) {
                "$Name=$Value"
                $found = $true
            }
            else {
                $line
            }
        }
    )
    if (-not $found) {
        $lines += "$Name=$Value"
    }

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($EnvPath, $lines, $utf8NoBom)
}

function Test-AdminShell {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Find-CaddyExecutable {
    $command = Get-Command caddy.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\caddy.exe"),
        (Join-Path $env:ProgramFiles "WinGet\Links\caddy.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    $packageRoots = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"),
        (Join-Path $env:ProgramFiles "WinGet\Packages")
    )
    foreach ($packageRoot in $packageRoots) {
        if (-not (Test-Path $packageRoot)) {
            continue
        }

        $match = Get-ChildItem -Path $packageRoot -Filter caddy.exe -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*CaddyServer.Caddy*" } |
            Select-Object -First 1
        if ($match) {
            return $match.FullName
        }
    }

    return $null
}

function Ensure-Caddy {
    $caddyExe = Find-CaddyExecutable
    if (-not $caddyExe) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (-not $winget) {
            throw "Caddy is not installed and winget is unavailable. Install Caddy from https://caddyserver.com/download and run setup again."
        }

        Write-Host "Installing Caddy with winget..."
        & winget install --id CaddyServer.Caddy -e --source winget --accept-package-agreements --accept-source-agreements | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Caddy installation failed."
        }

        $caddyExe = Find-CaddyExecutable
        if (-not $caddyExe) {
            throw "Caddy was installed, but caddy.exe could not be located. Open a new terminal and run setup again."
        }
    }

    $version = (& $caddyExe version | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0) {
        throw "The Caddy executable at '$caddyExe' could not be run."
    }
    Write-Host "Found Caddy $version"
    return $caddyExe
}

function Find-NssmExecutable {
    $command = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $bundledNssm = Join-Path $ProjectRoot "deployment\windows\nssm.exe"
    if (Test-Path $bundledNssm) {
        return $bundledNssm
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\nssm.exe"),
        (Join-Path $env:ProgramFiles "WinGet\Links\nssm.exe"),
        "C:\Tools\nssm\nssm.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    $packageRoots = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"),
        (Join-Path $env:ProgramFiles "WinGet\Packages")
    )
    foreach ($packageRoot in $packageRoots) {
        if (-not (Test-Path $packageRoot)) {
            continue
        }

        $match = Get-ChildItem -Path $packageRoot -Filter nssm.exe -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*NSSM.NSSM*" } |
            Select-Object -First 1
        if ($match) {
            return $match.FullName
        }
    }

    return $null
}

function Ensure-Nssm {
    $nssmExe = Find-NssmExecutable
    if (-not $nssmExe) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) {
            throw "NSSM is required for the Setuora Windows service, but WinGet is unavailable. Install App Installer from Microsoft Store and run setup again."
        }

        Write-Host "NSSM was not found. Installing it automatically with WinGet..."
        & $winget.Source install --id NSSM.NSSM -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "NSSM installation failed with exit code $LASTEXITCODE."
        }

        $nssmExe = Find-NssmExecutable
        if (-not $nssmExe) {
            throw "NSSM was installed, but nssm.exe could not be located."
        }
    }

    & $nssmExe version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The NSSM executable at '$nssmExe' could not be run."
    }

    # Keep a stable copy with the service installer so a WinGet link or package
    # update cannot invalidate the Windows service management path later.
    $stableNssm = Join-Path $ProjectRoot "deployment\windows\nssm.exe"
    $sourcePath = (Resolve-Path -LiteralPath $nssmExe).Path
    if ($sourcePath -ne $stableNssm) {
        Copy-Item -LiteralPath $sourcePath -Destination $stableNssm -Force
    }
    Write-Host "Found NSSM at $sourcePath"
    return $stableNssm
}

function Write-CaddyConfig {
    param(
        [string]$Address,
        [int]$UpstreamPort
    )

    New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null
    $lines = @(
        "https://${Address} {",
        "`ttls internal",
        "`tencode zstd gzip",
        "`treverse_proxy 127.0.0.1:${UpstreamPort}",
        "}"
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText(
        $Caddyfile,
        (($lines -join [Environment]::NewLine) + [Environment]::NewLine),
        $utf8NoBom
    )
}

function Grant-LocalServiceAccess {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Access
    )

    & icacls.exe $Path /grant "${LocalServiceSid}:(OI)(CI)$Access" /T /C | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not grant LocalService $Access access to '$Path'."
    }
}

function Remove-LegacyCaddyServices {
    foreach ($legacyName in $LegacyCaddyServiceNames) {
        if ($legacyName -eq $CaddyServiceName) {
            continue
        }

        $legacyService = Get-Service -Name $legacyName -ErrorAction SilentlyContinue
        if (-not $legacyService) {
            continue
        }

        if ($legacyService.Status -ne "Stopped") {
            Write-Host "Stopping old Caddy service '$legacyName'..."
            Stop-Service -Name $legacyName -Force
            $legacyService.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(15))
        }

        Write-Host "Removing old Caddy service '$legacyName'..."
        & sc.exe delete $legacyName | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Could not remove old Caddy service '$legacyName'."
        }

        for ($i = 0; $i -lt 20; $i++) {
            if (-not (Get-Service -Name $legacyName -ErrorAction SilentlyContinue)) {
                break
            }
            Start-Sleep -Milliseconds 500
        }

        if (Get-Service -Name $legacyName -ErrorAction SilentlyContinue) {
            throw "Old Caddy service '$legacyName' is still registered. Reboot Windows, then run scripts\setup.bat again."
        }
    }
}

function Assert-Win32ServiceResult {
    param(
        [Parameter(Mandatory=$true)]$Result,
        [Parameter(Mandatory=$true)][string]$Action
    )

    $returnCode = [int]$Result.ReturnValue
    if ($returnCode -eq 0) {
        return
    }

    $messages = @{
        2 = "Access denied"
        3 = "Dependent services running"
        8 = "Unknown failure"
        9 = "Path not found"
        10 = "Service already running"
        11 = "Service database locked"
        15 = "Service logon failure"
        16 = "Service marked for deletion"
        22 = "Invalid service account"
    }
    $message = $messages[$returnCode]
    if (-not $message) {
        $message = "Win32_Service returned an undocumented error"
    }

    throw "$Action (${returnCode}: $message)."
}

function Install-CaddyService {
    param([string]$CaddyExe)

    $serviceCaddyExe = Join-Path $CaddyDir "caddy.exe"
    Remove-LegacyCaddyServices
    $existingService = Get-Service -Name $CaddyServiceName -ErrorAction SilentlyContinue
    if ($existingService -and $existingService.Status -ne "Stopped") {
        Stop-Service -Name $CaddyServiceName -Force
        $existingService.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(15))
    }

    $sourcePath = (Resolve-Path $CaddyExe).Path
    if ($sourcePath -ne $serviceCaddyExe) {
        Copy-Item -LiteralPath $sourcePath -Destination $serviceCaddyExe -Force
    }

    & $serviceCaddyExe validate --config $Caddyfile --adapter caddyfile | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "The generated Caddy configuration is invalid."
    }

    $serviceCommand = "`"$serviceCaddyExe`" run --config `"$Caddyfile`" --adapter caddyfile"
    if ($existingService) {
        $cimService = Get-CimInstance -ClassName Win32_Service -Filter "Name='$CaddyServiceName'"
        $changeResult = Invoke-CimMethod `
            -InputObject $cimService `
            -MethodName Change `
            -Arguments @{
                PathName = $serviceCommand
                DisplayName = "Setuora Caddy HTTPS Proxy"
                StartMode = "Automatic"
                StartName = $CaddyServiceStartName
                StartPassword = $null
            }
        Assert-Win32ServiceResult -Result $changeResult -Action "Could not update the Caddy Windows service"
    }
    else {
        $createResult = Invoke-CimMethod `
            -ClassName Win32_Service `
            -MethodName Create `
            -Arguments @{
                Name = $CaddyServiceName
                DisplayName = "Setuora Caddy HTTPS Proxy"
                PathName = $serviceCommand
                # Win32_Service.Create declares ServiceType as CIM UInt8. An
                # uncast PowerShell integer is Int32, which Invoke-CimMethod
                # rejects with a type-mismatch error before creating the service.
                ServiceType = [byte]16
                StartMode = "Automatic"
                StartName = $CaddyServiceStartName
                StartPassword = $null
            }
        Assert-Win32ServiceResult -Result $createResult -Action "Could not create the Caddy Windows service"
    }

    # Caddy does not need LocalSystem privileges. It can read the proxy files and
    # write only its own certificate state; the app service gets separate access.
    Grant-LocalServiceAccess -Path $CaddyDir -Access "RX"

    & sc.exe description $CaddyServiceName "HTTPS reverse proxy for Setuora QR Tally Bridge" | Out-Null
    & sc.exe config $CaddyServiceName start= auto | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure the Caddy service to start automatically with Windows."
    }
    & sc.exe failure $CaddyServiceName reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure automatic recovery for the Caddy service."
    }
    & sc.exe failureflag $CaddyServiceName 1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not enable failure recovery for the Caddy service."
    }

    $stateDir = Join-Path $CaddyDir "state"
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
    Grant-LocalServiceAccess -Path $stateDir -Access "M"
    $serviceRegistryPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$CaddyServiceName"
    New-ItemProperty -Path $serviceRegistryPath -Name Environment -PropertyType MultiString -Value @(
        "XDG_DATA_HOME=$stateDir",
        "XDG_CONFIG_HOME=$stateDir"
    ) -Force | Out-Null

    Set-CaddyAppServiceDependency

    $firewallRuleName = "Setuora Caddy HTTPS"
    $existingFirewallRules = @(Get-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue)
    if ($existingFirewallRules.Count -gt 0) {
        $existingFirewallRules | Remove-NetFirewallRule
    }
    New-NetFirewallRule `
        -DisplayName $firewallRuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort 80, 443 `
        -RemoteAddress LocalSubnet `
        -Profile Any `
        -Enabled True | Out-Null

    try {
        Start-Service -Name $CaddyServiceName
        $service = Get-Service -Name $CaddyServiceName
        $service.WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
    }
    catch {
        throw "Caddy HTTPS could not start. Check whether another program uses ports 80 or 443, then review '$Caddyfile' and Windows Event Viewer. $($_.Exception.Message)"
    }

    $rootCertificate = Join-Path $stateDir "caddy\pki\authorities\local\root.crt"
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    while (-not (Test-Path $rootCertificate) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }

    $exportedCertificate = $null
    if (Test-Path $rootCertificate) {
        $exportedCertificate = Join-Path $CaddyDir "setuora-caddy-root.crt"
        Copy-Item -LiteralPath $rootCertificate -Destination $exportedCertificate -Force
        try {
            Import-Certificate -FilePath $rootCertificate -CertStoreLocation "Cert:\LocalMachine\Root" | Out-Null
        }
        catch {
            Write-Host "Caddy is running, but its root certificate could not be added to this PC's trust store." -ForegroundColor Yellow
        }
    }
    else {
        Write-Host "Caddy is running, but its root certificate was not ready to export yet." -ForegroundColor Yellow
    }

    return $exportedCertificate
}

function Offer-CaddySetup {
    if ($SkipCaddy) {
        return $null
    }

    $installCaddy = if ($ConfigureCaddy) { $true } else { Read-YesNo "Install and configure Caddy for HTTPS access from phones and laptops?" $true }
    if (-not $installCaddy) {
        return $null
    }

    if (-not (Test-AdminShell)) {
        Write-Host "Caddy setup needs Administrator access to create its service and firewall rule." -ForegroundColor Yellow
        Write-Host "Run scripts\setup.bat as Administrator to install and configure Caddy."
        return $null
    }

    $lanIp = Get-LocalIPv4
    $defaultAddress = if ($lanIp) { $lanIp } else { "setuora.local" }
    $address = Read-Default "HTTPS LAN IP address or local DNS name" $defaultAddress
    $address = $address -replace "^https://", ""
    $address = $address.TrimEnd("/")
    if ([string]::IsNullOrWhiteSpace($address) -or $address -match "^http://" -or $address -match "[/\s]") {
        throw "Use only a LAN IP address or DNS name for Caddy, for example 192.168.1.20 or setuora.local."
    }

    $caddyExe = Ensure-Caddy
    Write-CaddyConfig -Address $address -UpstreamPort $Port
    Set-EnvSetting -Name "SESSION_COOKIE_SECURE" -Value "true"
    Set-EnvSetting -Name "TRUSTED_HOSTS" -Value "$address,127.0.0.1,localhost,testserver"
    $rootCertificate = Install-CaddyService -CaddyExe $caddyExe
    return @{
        Address = $address
        RootCertificate = $rootCertificate
    }
}

function Set-CaddyAppServiceDependency {
    $appService = Get-Service -Name $AppServiceName -ErrorAction SilentlyContinue
    $caddyService = Get-Service -Name $CaddyServiceName -ErrorAction SilentlyContinue
    if (-not $appService -or -not $caddyService) {
        return
    }

    & sc.exe config $CaddyServiceName depend= $AppServiceName | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure Caddy to wait for the Setuora service during Windows startup."
    }
}

function Get-RecentServiceError {
    if (-not (Test-Path -LiteralPath $LogsDir)) {
        return $null
    }

    $errorLog = Get-ChildItem -LiteralPath $LogsDir -Filter "setuora-err*.log" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $errorLog) {
        return $null
    }

    $lines = @(
        Get-Content -LiteralPath $errorLog.FullName -Tail 15 -ErrorAction SilentlyContinue |
            Where-Object { $_.Trim() -ne "" }
    )
    if ($lines.Count -eq 0) {
        return $null
    }
    return ($lines -join [Environment]::NewLine)
}

function Get-ManagedServiceFailureMessage {
    param(
        [Parameter(Mandatory=$true)][string]$Summary,
        [switch]$IncludeSetuoraLog,
        [string]$ExceptionMessage
    )

    $message = $Summary
    if ($IncludeSetuoraLog) {
        $details = Get-RecentServiceError
        if ($details) {
            $message += [Environment]::NewLine + "Recent service log:" + [Environment]::NewLine + $details
        }
    }
    if ($ExceptionMessage) {
        $message += [Environment]::NewLine + $ExceptionMessage
    }
    return $message
}

function Start-ManagedService {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$DisplayName,
        [switch]$IncludeSetuoraLog
    )

    $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $service) {
        throw "$DisplayName service '$Name' is not installed."
    }
    if ($service.Status -ne "Running") {
        try {
            Start-Service -Name $Name
            $service.WaitForStatus("Running", [TimeSpan]::FromSeconds(30))
        }
        catch {
            throw (Get-ManagedServiceFailureMessage `
                -Summary "$DisplayName could not start. Check Windows Event Viewer and the Setuora logs." `
                -IncludeSetuoraLog:$IncludeSetuoraLog `
                -ExceptionMessage $_.Exception.Message)
        }
    }
    $service.Refresh()
    if ($service.Status -ne "Running") {
        throw (Get-ManagedServiceFailureMessage `
            -Summary "$DisplayName did not remain running after startup." `
            -IncludeSetuoraLog:$IncludeSetuoraLog)
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
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [Parameter(Mandatory=$true)][string]$DisplayName
    )

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

function Offer-ServiceInstall {
    $existingService = Get-Service -Name $AppServiceName -ErrorAction SilentlyContinue
    if ($existingService) {
        Write-Host "Existing Setuora Windows service found. Updating its automatic-start configuration."
        if (-not (Test-AdminShell)) {
            throw "Updating the existing Windows service needs Administrator access. Right-click scripts\setup.bat and choose 'Run as administrator'."
        }

        $nssmPath = Ensure-Nssm
        $serviceScript = Join-Path $ProjectRoot "deployment\windows\install_service.ps1"
        & powershell -NoProfile -ExecutionPolicy Bypass -File $serviceScript -ProjectDir $ProjectRoot -NssmPath $nssmPath -Port $Port
        if ($LASTEXITCODE -ne 0) {
            throw "Windows service update failed."
        }
        Set-CaddyAppServiceDependency
        return $true
    }

    if ($Repair) {
        Write-Host "No Setuora Windows service is installed; keeping the current console-server setup."
        return $false
    }

    $installService = Read-YesNo "Install Setuora as an automatic Windows service?" $true
    if (-not $installService) {
        return $false
    }

    if (-not (Test-AdminShell)) {
        throw "Windows service installation needs Administrator access. Right-click scripts\setup.bat, choose 'Run as administrator', and run it again."
    }

    $nssmPath = Ensure-Nssm
    $serviceScript = Join-Path $ProjectRoot "deployment\windows\install_service.ps1"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $serviceScript -ProjectDir $ProjectRoot -NssmPath $nssmPath -Port $Port
    if ($LASTEXITCODE -ne 0) {
        throw "Windows service install failed."
    }
    Set-CaddyAppServiceDependency
    return $true
}

function Get-LocalIPv4 {
    $addresses = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName())
    foreach ($address in $addresses) {
        if (
            $address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
            -not $address.ToString().StartsWith("127.") -and
            -not $address.ToString().StartsWith("169.254.")
        ) {
            return $address.ToString()
        }
    }
    return $null
}

Write-Section "Setuora Setup"
if ($Repair) {
    Write-Host "Repair mode will validate Python, rebuild damaged runtime files, reinstall verified packages, and test the app. Your .env and data are preserved."
}
else {
    Write-Host "This setup will prepare Python, install packages, create .env, and configure optional services. Start the app separately when you are ready."
}

Set-Location $ProjectRoot
New-Item -ItemType Directory -Force -Path $DataDir, $LogsDir | Out-Null

if ($Repair -and (Test-Path $ProcessHelper)) {
    . $ProcessHelper
    $existingService = Get-Service -Name $AppServiceName -ErrorAction SilentlyContinue
    $restartAsService = [bool]($existingService -and $existingService.Status -ne "Stopped")
    if (-not $restartAsService) {
        $runningSetuoraProcesses = @(Get-SetuoraServerProcesses -ProjectRoot $ProjectRoot -ExcludeProcessIds @($PID))
        $restartAsConsole = $runningSetuoraProcesses.Count -gt 0
        if ($restartAsConsole) {
            $launchInfo = Get-SetuoraServerLaunchInfo -Process $runningSetuoraProcesses[0] -DefaultHostAddress $restartHostAddress -DefaultPort $restartPort
            $restartHostAddress = $launchInfo.HostAddress
            $restartPort = $launchInfo.Port
        }
    }
}

Write-Section "Stop Existing Server"
if ((Test-Path $VenvPython) -or $Repair) {
    if (-not (Test-Path $StopScript)) {
        throw "The server management helper is missing: '$StopScript'."
    }

    $existingSetuoraService = Get-Service -Name $AppServiceName -ErrorAction SilentlyContinue
    if (
        $existingSetuoraService -and
        $existingSetuoraService.Status -ne "Stopped" -and
        -not (Test-AdminShell)
    ) {
        throw "Setuora is running as a Windows service. Right-click scripts\setup.bat, choose 'Run as administrator', and try again."
    }

    & $StopScript -ProjectDir $ProjectRoot
}
else {
    Write-Host "Fresh installation; there is no existing Setuora server to stop."
}

Write-Section "Python"
$python = Ensure-Python
Ensure-Venv $python

Write-Section "Dependencies"
Install-Dependencies

Write-Section "Configuration"
if ($Repair -and (Test-Path $EnvPath)) {
    if (Test-EnvFileHasSafeBootstrapPassword) {
        Write-Host "Existing .env settings preserved."
        $credentials = $null
    }
    else {
        Write-Host "The preserved .env has no safe first-admin password; Setuora cannot start until one is set." -ForegroundColor Yellow
        $credentials = Ensure-EnvFile
    }
}
else {
    $credentials = Ensure-EnvFile
}

Write-Section "Smoke Test"
& $VenvPython -c "import uvicorn; from app.main import app; print('App import OK')"
if ($LASTEXITCODE -ne 0) {
    throw "The app could not be imported. Check the error above."
}

if ($Repair) {
    Write-Section "Regression Tests"
    & $VenvPython -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        throw "Repair completed the runtime checks, but the application test suite failed. Review the error above before starting Setuora."
    }
}

Write-Section "Windows Autostart"
$serviceInstalled = Offer-ServiceInstall

Write-Section "HTTPS with Caddy"
if ($Repair) {
    $existingCaddyService = Get-Service -Name $CaddyServiceName -ErrorAction SilentlyContinue
    if ($existingCaddyService) {
        if (-not (Test-Path -LiteralPath $Caddyfile)) {
            throw "The Caddy service exists, but its configuration is missing: '$Caddyfile'. Run Setuora.exe setup to configure LAN HTTPS again."
        }
        $managedCaddyExe = Join-Path $CaddyDir "caddy.exe"
        if (-not (Test-Path -LiteralPath $managedCaddyExe)) {
            $managedCaddyExe = Ensure-Caddy
        }
        Install-CaddyService -CaddyExe $managedCaddyExe | Out-Null
        Write-Host "Existing Caddy HTTPS service repaired and configured for automatic startup."
    }
    else {
        Write-Host "No Caddy HTTPS service is installed. Run Setuora.exe setup to enable phone and laptop access." -ForegroundColor Yellow
    }
    $caddySetup = $null
}
else {
    $caddySetup = Offer-CaddySetup
}

if ($serviceInstalled) {
    Set-CaddyAppServiceDependency
    Start-ManagedService -Name $AppServiceName -DisplayName "Setuora" -IncludeSetuoraLog
    $installedCaddyService = Get-Service -Name $CaddyServiceName -ErrorAction SilentlyContinue
    if ($installedCaddyService) {
        Start-ManagedService -Name $CaddyServiceName -DisplayName "Setuora Caddy HTTPS proxy"
        $startupCaddyAddress = Get-CaddyAddress
        if (-not $startupCaddyAddress) {
            throw "Caddy is installed, but no HTTPS address could be read from '$Caddyfile'."
        }
    }
    else {
        Write-Host "Setuora is running, but Caddy is not installed; phone and laptop HTTPS access is unavailable." -ForegroundColor Yellow
    }
    Wait-HealthEndpoint -Uri "http://127.0.0.1:$Port/health" -DisplayName "Setuora local health check"
    if ($installedCaddyService) {
        Wait-HealthEndpoint -Uri "https://$startupCaddyAddress/health" -DisplayName "Setuora Caddy HTTPS"
    }
}

Write-Section "Done"
if ($Repair) {
    Write-Host "Repair completed successfully." -ForegroundColor Green
}
else {
    Write-Host "Setup completed successfully." -ForegroundColor Green
}
Write-Host "Local URL: http://127.0.0.1:$Port"
$lanIp = Get-LocalIPv4
$caddyAddress = if ($caddySetup) { $caddySetup.Address } else { Get-CaddyAddress }
if ($caddyAddress) {
    Write-Host "Secure LAN URL: https://$caddyAddress" -ForegroundColor Green
    if ($caddySetup -and $caddySetup.RootCertificate) {
        Write-Host "Phone certificate: $($caddySetup.RootCertificate)"
        Write-Host "Install this certificate as a trusted CA certificate on every phone and laptop that uses Setuora."
    }
}
elseif ($lanIp) {
    Write-Host "LAN test URL: http://${lanIp}:$Port"
}
Write-Host "Backup reminder: include data, .env, and deployment\caddy\state in scheduled server backups."
if ($credentials) {
    Write-Host ""
    Write-Host "First admin login:"
    Write-Host "  Username: $($credentials.Username)"
    Write-Host "  Password: $($credentials.Password)"
    Write-Host "Keep this password somewhere safe. It is only shown during setup."
}

if ($Repair) {
    if ($serviceInstalled) {
        Write-Host "Setuora and Caddy were repaired, started, and configured for Windows autostart." -ForegroundColor Green
    }
    elseif ($restartAsConsole) {
        Start-Process -FilePath $StartScript -ArgumentList @("-HostAddress", "$restartHostAddress", "-Port", "$restartPort", "--console-only")
        Write-Host "Setuora was repaired and restarted in a new window." -ForegroundColor Green
    }
    else {
        Write-Host "Setuora was stopped before repair, so it remains stopped."
    }
}
elseif ($serviceInstalled) {
    Write-Host "Setuora and Caddy are running and will start automatically with Windows." -ForegroundColor Green
}
elseif (-not $SkipStart) {
    $startNow = Read-YesNo "Start Setuora and Caddy now in a new window?" $true
    if ($startNow) {
        Start-Process -FilePath $StartScript -ArgumentList @("-Port", "$Port")
        Start-Sleep -Seconds 2
        if ($caddySetup) {
            Start-Process "https://$($caddySetup.Address)"
        }
        else {
            Start-Process "http://127.0.0.1:$Port"
        }
    }
}
