param([Parameter(Mandatory=$true)][string]$Stage)
$ErrorActionPreference = 'Stop'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $env:PSModulePath
$stageRoot = (Resolve-Path -LiteralPath $Stage).Path
$manifest = Get-Content -LiteralPath (Join-Path $stageRoot 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$packageRoot = [IO.Path]::GetFullPath($manifest.package).TrimEnd('\')
$candidate = Join-Path $stageRoot 'next'
$previous = Join-Path $stageRoot 'previous'
$statusPath = Join-Path $stageRoot 'status.json'
$moved = $false
function Status([string]$state, [string]$message = '') {
    [IO.File]::WriteAllText($statusPath, (@{state=$state; message=$message; version=$manifest.version} | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
}
function Check-Path([string]$value, [string]$root) {
    $full = [IO.Path]::GetFullPath($value)
    if (-not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid update path' }
    $probe = $full
    while ($probe.Length -ge $root.Length) {
        if ((Test-Path -LiteralPath $probe) -and ((Get-Item -LiteralPath $probe -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked update path' }
        if ($probe -eq $root) { break }
        $probe = Split-Path -Parent $probe
    }
}
try {
    $parent = Split-Path -Parent $packageRoot
    Check-Path $packageRoot $parent
    Check-Path $stageRoot (Join-Path $parent '.xuloom-updates')
    if (Test-Path -LiteralPath $previous) { throw 'Update has already been applied' }
    foreach ($file in $manifest.hashes.PSObject.Properties) {
        $target = Join-Path $candidate $file.Name
        Check-Path $target $candidate
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $file.Value) { throw 'Staged file checksum mismatch' }
    }
    Status 'waiting'
    $deadline = (Get-Date).AddHours(12)
    do {
        $active = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($packageRoot + '\', [StringComparison]::OrdinalIgnoreCase) }
        if (-not $active) { break }
        if ((Get-Date) -gt $deadline) { throw 'Application did not exit; update not applied' }
        Start-Sleep -Seconds 1
    } while ($true)
    if ((Get-FileHash -LiteralPath (Join-Path $packageRoot 'Xuloom.exe') -Algorithm SHA256).Hash -ne $manifest.originalExeSha256) { throw 'Installation changed after staging' }
    Move-Item -LiteralPath $packageRoot -Destination $previous
    $moved = $true
    Move-Item -LiteralPath $candidate -Destination $packageRoot
    Status 'complete'
} catch {
    $failure = $_.Exception.Message
    if ($moved -and -not (Test-Path -LiteralPath $packageRoot)) { Move-Item -LiteralPath $previous -Destination $packageRoot }
    Status 'failed' $failure
}
