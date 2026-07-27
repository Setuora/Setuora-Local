param(
    [Parameter(Mandatory=$true)][string]$ProjectDir,
    [Parameter(Mandatory=$true)][string]$Address,
    [int]$Port = 8000,
    [string]$AppServiceName = "SetuoraQrTallyBridge",
    [string]$CaddyServiceName = "SetuoraCaddy"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = [IO.Path]::GetFullPath($ProjectDir).TrimEnd("\")
$pythonExe = Join-Path $projectRoot ".venv\Scripts\python.exe"
$envPath = Join-Path $projectRoot ".env"
$lockPath = Join-Path $projectRoot "requirements.lock"
$caddyExe = Join-Path $projectRoot "deployment\caddy\caddy.exe"
$caddyfile = Join-Path $projectRoot "deployment\caddy\Caddyfile"

function Assert-Check {
    param([bool]$Condition, [string]$Message)

    if (-not $Condition) {
        throw "PRE-FLIGHT FAILED: $Message"
    }
}

function Read-EnvSettings {
    param([string]$Path)

    $settings = @{}
    foreach ($rawLine in Get-Content -LiteralPath $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }
        $parts = $line.Split("=", 2)
        $settings[$parts[0].Trim()] = $parts[1].Trim()
    }
    return $settings
}

function Test-HealthEndpoint {
    param([string]$Uri)

    $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 15
    Assert-Check ($response.StatusCode -eq 200) "Health endpoint did not return HTTP 200: $Uri"
    $body = $response.Content | ConvertFrom-Json
    Assert-Check ($body.status -eq "ok") "Health endpoint did not report status=ok: $Uri"
    return $response
}

function Assert-LocalServiceIdentity {
    param([string]$ServiceName)

    $service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
    Assert-Check ($null -ne $service) "Windows service '$ServiceName' is not installed."
    Assert-Check ($service.State -eq "Running") "Windows service '$ServiceName' is not running."
    Assert-Check ($service.StartMode -eq "Auto") "Windows service '$ServiceName' is not configured for automatic startup."
    Assert-Check ($service.StartName -eq "NT AUTHORITY\LocalService") (
        "Windows service '$ServiceName' runs as '$($service.StartName)', not the least-privilege LocalService account."
    )
}

Assert-Check (Test-Path -LiteralPath $pythonExe) "Virtual-environment Python was not found."
Assert-Check (Test-Path -LiteralPath $envPath) ".env was not found."
Assert-Check (Test-Path -LiteralPath $lockPath) "requirements.lock was not found."
Assert-Check (Test-Path -LiteralPath $caddyExe) "Managed Caddy executable was not found."
Assert-Check (Test-Path -LiteralPath $caddyfile) "Generated Caddyfile was not found."

$settings = Read-EnvSettings -Path $envPath
Assert-Check ($settings["SESSION_COOKIE_SECURE"] -eq "true") "SESSION_COOKIE_SECURE must be true for HTTPS deployment."
Assert-Check ($settings.ContainsKey("BOOTSTRAP_ADMIN_PASSWORD")) "BOOTSTRAP_ADMIN_PASSWORD is missing."
Assert-Check ($settings["BOOTSTRAP_ADMIN_PASSWORD"].Length -ge 8 -and $settings["BOOTSTRAP_ADMIN_PASSWORD"] -ne "admin123") (
    "The first admin password is missing, too short, or insecure."
)
Assert-Check ($settings.ContainsKey("TRUSTED_HOSTS") -and ($settings["TRUSTED_HOSTS"].Split(",") -contains $Address)) (
    "TRUSTED_HOSTS does not include '$Address'."
)

$dirty = @(& git -C $projectRoot status --porcelain)
Assert-Check ($LASTEXITCODE -eq 0) "Git could not inspect the release worktree."
Assert-Check ($dirty.Count -eq 0) "The release worktree has uncommitted changes."

Assert-LocalServiceIdentity -ServiceName $AppServiceName
Assert-LocalServiceIdentity -ServiceName $CaddyServiceName
$caddyService = Get-Service -Name $CaddyServiceName
Assert-Check (($caddyService.ServicesDependedOn | ForEach-Object { $_.Name }) -contains $AppServiceName) (
    "Caddy is not configured to wait for the Setuora service during Windows startup."
)

& $caddyExe validate --config $caddyfile --adapter caddyfile | Out-Host
Assert-Check ($LASTEXITCODE -eq 0) "Caddy configuration validation failed."

& $pythonExe -m pip check | Out-Host
Assert-Check ($LASTEXITCODE -eq 0) "Python dependencies are inconsistent."

& $pythonExe -m pytest -q | Out-Host
Assert-Check ($LASTEXITCODE -eq 0) "The release test suite failed."

$localHealth = Test-HealthEndpoint -Uri "http://127.0.0.1:$Port/health"
$httpsHealth = Test-HealthEndpoint -Uri "https://$Address/health"
foreach ($header in @("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options", "Strict-Transport-Security")) {
    Assert-Check (-not [string]::IsNullOrWhiteSpace($httpsHealth.Headers[$header])) "HTTPS response is missing the $header security header."
}

& $pythonExe -c "from app.services.backup import create_scheduled_backup; print(create_scheduled_backup().path)" | Out-Host
Assert-Check ($LASTEXITCODE -eq 0) "A verified SQLite backup could not be created."

$firewallRule = Get-NetFirewallRule -DisplayName "Setuora Caddy HTTPS" -ErrorAction SilentlyContinue
Assert-Check ($null -ne $firewallRule -and $firewallRule.Enabled -eq "True") "The Setuora Caddy HTTPS firewall rule is not enabled."

Write-Host "Production pre-flight passed: HTTPS, service identity, release state, tests, health checks, and a verified backup are all valid." -ForegroundColor Green
