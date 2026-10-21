param(
    [Parameter(Mandatory=$true)][string]$ProjectDir,
    [string]$ServiceName = "SetuoraQrTallyBridge",
    [string]$CaddyServiceName = "SetuoraCaddy"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = [IO.Path]::GetFullPath($ProjectDir).TrimEnd("\")
$recoveryRoot = Join-Path $env:ProgramData "Setuora\Recovery"
$productRecovery = Join-Path $recoveryRoot "Local"
$stopScript = Join-Path $PSScriptRoot "stop_setuora.ps1"
$startScript = Join-Path $PSScriptRoot "start_setuora.ps1"
$finisher = Join-Path $PSScriptRoot "finish_uninstall.ps1"
$exportedCertificate = Join-Path $projectRoot "deployment\caddy\setuora-caddy-root.crt"
$firewallRuleName = "Setuora Caddy HTTPS"
# Settings, the database and its backups, logs, and the Caddy HTTPS identity.
$persistentPaths = @(
    ".env",
    "data",
    "logs",
    "deployment\caddy\Caddyfile",
    "deployment\caddy\state",
    "deployment\caddy\setuora-caddy-root.crt"
)

function Assert-PlainPath([string]$Path) {
    if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "A linked path cannot be used for recovery or removal: $Path"
    }
}

function Protect-RecoveryDirectory([string]$Path) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    Assert-PlainPath $Path
    & icacls.exe $Path /inheritance:r /remove:g "*S-1-1-0" "*S-1-5-11" "*S-1-5-32-545" /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /Q /L | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Recovery folder permissions could not be secured: $Path"
    }
}

function Get-DatabaseRelativePaths {
    # A database configured inside the installation but outside data\ would be
    # deleted with the application files, so keep it too.
    $envFile = Join-Path $projectRoot ".env"
    if (-not (Test-Path -LiteralPath $envFile)) {
        return @()
    }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -notmatch '^\s*DATABASE_URL\s*=\s*["'']?sqlite:///([^"'']+)["'']?\s*$') {
            continue
        }
        $database = $Matches[1]
        if (-not [IO.Path]::IsPathRooted($database)) {
            $database = Join-Path $projectRoot $database
        }
        $database = [IO.Path]::GetFullPath($database)
        $prefix = $projectRoot + "\"
        if (-not $database.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            return @()
        }
        return @(
            @($database, "$database-wal", "$database-shm") |
                Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
                ForEach-Object { $_.Substring($prefix.Length) }
        )
    }
    return @()
}

function Copy-VerifiedRecovery([string]$Relative, [string]$Bundle) {
    $source = Join-Path $projectRoot $Relative
    $target = Join-Path $Bundle $Relative
    Assert-PlainPath $source
    $item = Get-Item -LiteralPath $source -Force
    $files = @($item)
    if ($item.PSIsContainer) {
        foreach ($child in @(Get-ChildItem -LiteralPath $source -Recurse -Force)) {
            Assert-PlainPath $child.FullName
        }
        $files = @(Get-ChildItem -LiteralPath $source -File -Recurse -Force)
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
    foreach ($file in $files) {
        $copy = Join-Path $Bundle $file.FullName.Substring($projectRoot.Length + 1)
        if (-not (Test-Path -LiteralPath $copy -PathType Leaf) -or
            (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $copy -Algorithm SHA256).Hash) {
            throw "Recovery checksum failed for $($file.FullName); application files were left in place."
        }
    }
}

function Remove-ServiceIfPresent([string]$Name) {
    if (-not (Get-Service -Name $Name -ErrorAction SilentlyContinue)) {
        return
    }
    & sc.exe delete $Name | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The '$Name' Windows service could not be removed."
    }
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-Service -Name $Name -ErrorAction SilentlyContinue)) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "The '$Name' Windows service is still registered. Close Windows Services, then retry Uninstall."
}

function Remove-CaddyTrust {
    if (-not (Test-Path -LiteralPath $exportedCertificate -PathType Leaf)) {
        return
    }
    try {
        $certificate = New-Object Security.Cryptography.X509Certificates.X509Certificate2 $exportedCertificate
        $trusted = Join-Path "Cert:\LocalMachine\Root" $certificate.Thumbprint
        if (Test-Path -LiteralPath $trusted) {
            Remove-Item -LiteralPath $trusted -Force
        }
    }
    catch {
        Write-Host "The Setuora HTTPS root certificate could not be removed from this PC's trust store. $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

$stopAttempted = $false
$cleanupStarted = $false
try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Approve the Administrator prompt before removing Setuora."
    }
    foreach ($marker in @("app\main.py", "scripts\setup.bat", "deployment\windows\server_processes.ps1")) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $marker) -PathType Leaf)) {
            throw "Uninstall is available only for a Setuora installation; $projectRoot was left untouched."
        }
    }
    Assert-PlainPath $projectRoot
    foreach ($required in @($stopScript, $finisher)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "The installed uninstall helpers are incomplete. Choose Start to repair them, then retry."
        }
    }

    Write-Host "This removes Setuora, its Windows services and its HTTPS settings from $projectRoot." -ForegroundColor Yellow
    Write-Host "Settings, the database, backups and logs will be kept in a verified recovery folder."
    $answer = Read-Host "Type REMOVE Local to continue"
    if ($answer -cne "REMOVE Local") {
        Write-Host "Uninstall cancelled."
        exit 1
    }

    $stopAttempted = $true
    & $stopScript -ProjectDir $projectRoot -ServiceName $ServiceName -CaddyServiceName $CaddyServiceName -DisableAutostart

    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
    $bundle = Join-Path $productRecovery ("$stamp-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    foreach ($path in @($recoveryRoot, $productRecovery, $bundle)) {
        Protect-RecoveryDirectory $path
    }
    Write-Host "Saving settings and data in $bundle"
    $preserved = New-Object System.Collections.Generic.List[string]
    foreach ($relative in @($persistentPaths + @(Get-DatabaseRelativePaths))) {
        $covered = @($preserved | Where-Object { $relative.StartsWith("$_\", [StringComparison]::OrdinalIgnoreCase) -or $relative -ieq $_ })
        if ($covered.Count -gt 0 -or -not (Test-Path -LiteralPath (Join-Path $projectRoot $relative))) {
            continue
        }
        Copy-VerifiedRecovery -Relative $relative -Bundle $bundle
        $preserved.Add($relative)
    }
    @{
        format = "setuora-local-uninstall-recovery-v1"
        installedRoot = $projectRoot
        capturedAt = (Get-Date).ToUniversalTime().ToString("o")
        preserved = @($preserved)
    } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $bundle "recovery-inventory.json") -Encoding UTF8
    Protect-RecoveryDirectory $bundle

    # Remove the installation only after every recovery copy was verified.
    Remove-ServiceIfPresent $CaddyServiceName
    Remove-ServiceIfPresent $ServiceName
    Get-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Remove-CaddyTrust

    # Setuora.exe stays open until the control window closes, so a helper in
    # the recovery folder removes the application files afterwards.
    $cleanup = Join-Path $bundle "finish_uninstall.ps1"
    Copy-Item -LiteralPath $finisher -Destination $cleanup
    $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + $cleanup + '" -InstallRoot "' + $projectRoot + '"'
    Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WorkingDirectory $bundle -WindowStyle Hidden
    $cleanupStarted = $true
    Write-Host "Recovery saved to $bundle" -ForegroundColor Green
    Write-Host "Application files are removed after the Setuora windows close."
    Write-Host "The result is written to $(Join-Path $bundle 'removal-status.txt')."
    Write-Host "Shared Python and Git installations remain available to other applications."
    exit 0
}
catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($stopAttempted -and -not $cleanupStarted -and (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) -and
        (Test-Path -LiteralPath $startScript)) {
        Write-Host "Restarting Setuora after the cancelled removal..." -ForegroundColor Yellow
        try {
            & $startScript -ProjectDir $projectRoot -ServiceName $ServiceName -CaddyServiceName $CaddyServiceName
        }
        catch {
            Write-Host "Automatic restart failed. Open Setuora.exe and choose Start." -ForegroundColor Yellow
        }
    }
    Write-Host "Application files remain in place. Check the message above before retrying Uninstall."
    exit 1
}
