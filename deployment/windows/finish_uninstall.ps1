param(
    [Parameter(Mandatory=$true)][string]$InstallRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$target = [IO.Path]::GetFullPath($InstallRoot).TrimEnd("\")
$statusFile = Join-Path $PSScriptRoot "removal-status.txt"

try {
    Set-Location -LiteralPath $PSScriptRoot
    if (Test-Path -LiteralPath $target) {
        if ((Get-Item -LiteralPath $target -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing to remove a linked installation folder: $target"
        }
        if (-not (Test-Path -LiteralPath (Join-Path $target "app\main.py") -PathType Leaf)) {
            throw "Refusing to remove a folder that is not a Setuora installation: $target"
        }
    }

    # Wait until programs started from the installation, such as Setuora.exe,
    # have closed; Windows cannot delete a running program.
    $prefix = $target + "\"
    $deadline = [DateTime]::UtcNow.AddMinutes(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        $running = @(
            Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }
        )
        if ($running.Count -eq 0) {
            break
        }
        Start-Sleep -Seconds 2
    }

    $removed = $false
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        try {
            if (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
            }
            $removed = -not (Test-Path -LiteralPath $target)
            if ($removed) {
                break
            }
        }
        catch {
            if ($attempt -eq 44) {
                throw
            }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $removed) {
        throw "Application files could not be removed from $target"
    }
    "Removed Setuora application files at $(Get-Date -Format o). Recovery folder: $PSScriptRoot" |
        Set-Content -LiteralPath $statusFile -Encoding UTF8
}
catch {
    "Removal needs attention at $(Get-Date -Format o): $($_.Exception.Message)" |
        Set-Content -LiteralPath $statusFile -Encoding UTF8
    exit 1
}
