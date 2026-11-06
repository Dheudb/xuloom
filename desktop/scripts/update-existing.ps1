param(
    [Parameter(Mandatory = $true)][string]$PackageDirectory,
    [Parameter(Mandatory = $true)][string]$StagedResources,
    [int]$WaitSeconds = 43200
)
$ErrorActionPreference = 'Stop'
$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $env:PSModulePath
$packageRoot = (Resolve-Path -LiteralPath $PackageDirectory).Path.TrimEnd('\')
$stageRoot = (Resolve-Path -LiteralPath $StagedResources).Path.TrimEnd('\')
$statusFile = Join-Path $stageRoot 'status.json'
function Write-Status([string]$state, [string]$message = '') {
    $json = @{ state = $state; message = $message; packageDirectory = $packageRoot; updatedAt = (Get-Date).ToString('o') } | ConvertTo-Json
    [System.IO.File]::WriteAllText($statusFile, $json, [System.Text.UTF8Encoding]::new($false))
}
function Assert-Child([string]$candidate, [string]$root) {
    $full = [System.IO.Path]::GetFullPath($candidate)
    if (-not $full.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Update path is outside the intended directory.' }
    $probe = $full
    while ($probe.Length -ge $root.Length) {
        if (Test-Path -LiteralPath $probe) {
            if ((Get-Item -LiteralPath $probe -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Refusing to update a linked directory.' }
        }
        if ($probe -eq $root) { break }
        $probe = Split-Path -Parent $probe
    }
}
$changes = [System.Collections.Generic.List[object]]::new()
try {
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot 'Xuloom.exe') -PathType Leaf)) { throw 'The original Xuloom.exe is missing.' }
    $manifest = Get-Content -LiteralPath (Join-Path $stageRoot 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ((Get-FileHash -LiteralPath (Join-Path $packageRoot 'Xuloom.exe') -Algorithm SHA256).Hash -ne $manifest.originalExeSha256) { throw 'The installation executable changed since staging.' }
    if ($manifest.resourceHashes) {
        foreach ($resource in $manifest.resourceHashes.PSObject.Properties) {
            $resourcePath = Join-Path $stageRoot $resource.Name
            Assert-Child $resourcePath $stageRoot
            if ((Get-FileHash -LiteralPath $resourcePath -Algorithm SHA256).Hash -ne $resource.Value) { throw 'Staged resource does not match its build manifest.' }
        }
    }
    if ($manifest.metadataDirectory -notmatch '^xuloom-[0-9A-Za-z_.-]+\.dist-info$') { throw 'Invalid Python package metadata directory.' }
    $entries = @(
        @{ Source = (Join-Path $stageRoot 'app'); Target = (Join-Path $packageRoot 'resources\app'); Backup = (Join-Path $stageRoot 'backup\app') },
        @{ Source = (Join-Path $stageRoot 'xuloom'); Target = (Join-Path $packageRoot 'resources\python\Lib\site-packages\xuloom'); Backup = (Join-Path $stageRoot 'backup\xuloom') },
        @{ Source = (Join-Path $stageRoot 'metadata'); Target = (Join-Path $packageRoot ('resources\python\Lib\site-packages\' + $manifest.metadataDirectory)); Backup = (Join-Path $stageRoot 'backup\metadata') }
    )
    foreach ($dependency in $manifest.pythonPackages) {
        if ($dependency -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$' -or $dependency -in @('xuloom', $manifest.metadataDirectory)) { throw 'Invalid Python dependency directory.' }
        if ($entries | Where-Object { (Split-Path -Leaf $_.Target) -eq $dependency }) { throw 'Duplicate Python dependency directory.' }
        $entries += @{ Source = (Join-Path $stageRoot ('dependencies\' + $dependency)); Target = (Join-Path $packageRoot ('resources\python\Lib\site-packages\' + $dependency)); Backup = (Join-Path $stageRoot ('backup\dependencies\' + $dependency)) }
    }
    foreach ($entry in $entries) {
        Assert-Child $entry.Source $stageRoot
        Assert-Child $entry.Target $packageRoot
        Assert-Child $entry.Backup $stageRoot
        if (-not (Test-Path -LiteralPath $entry.Source -PathType Container)) { throw 'The update payload is incomplete.' }
        if (Test-Path -LiteralPath $entry.Backup) { throw 'This update was already applied or needs recovery.' }
        $hashes = @{}
        foreach ($file in Get-ChildItem -LiteralPath $entry.Source -File -Recurse) {
            Assert-Child $file.FullName $stageRoot
            if ($manifest.resourceHashes) {
                $resourceName = $file.FullName.Substring($stageRoot.Length + 1).Replace('\', '/')
                if (-not $manifest.resourceHashes.PSObject.Properties[$resourceName]) { throw 'Unexpected resource outside the build manifest.' }
            }
            $hashes[$file.FullName.Substring($entry.Source.Length + 1)] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        }
        $entry.Hashes = $hashes
    }
    Write-Status 'waiting_for_exit'
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    do {
        $running = @(Get-CimInstance Win32_Process -Filter "Name = 'Xuloom.exe' OR Name = 'python.exe'" | Where-Object {
            $_.ExecutablePath -and $_.ExecutablePath.StartsWith($packageRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)
        })
        if ($running.Count -eq 0) { break }
        if ((Get-Date) -gt $deadline) { throw 'Xuloom is still open. The update payload is preserved for retry.' }
        Start-Sleep -Seconds 1
    } while ($true)
    # Verify every removal before mutation; retire only hashed Python resources.
    # Native libraries, launchers, localization and licenses are never candidates.
    $retirements = @()
    $retirementNames = @{}
    foreach ($file in $manifest.retiredFiles) {
        if ($file.path -notmatch '^resources/python/Lib/' -or $file.path -match '\\|(^|/)\.\.(/|$)' -or
            $file.path -match '(?i)\.(dll|pyd|exe|so|dylib|pak|pem|crt|cer|der|pfx|p12|ttf|otf|woff|woff2)$|license|copying|copyright|notice' -or
            $file.sha256 -notmatch '^[a-f0-9]{64}$' -or $retirementNames.ContainsKey($file.path)) { throw 'Invalid retired resource.' }
        $retirementNames[$file.path] = $true
        $target = Join-Path $packageRoot $file.path
        $backup = Join-Path $stageRoot ('backup\retired-resources\' + $file.path)
        Assert-Child $target $packageRoot
        Assert-Child $backup $stageRoot
        if ($entries | Where-Object { $target.StartsWith($_.Target + '\', [System.StringComparison]::OrdinalIgnoreCase) }) { throw 'Retirement overlaps a replacement.' }
        if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Test-Path -LiteralPath $backup) -or
            (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $file.sha256) { throw 'Retired resource changed since staging.' }
        $retirements += @{ Target = $target; Backup = $backup }
    }
    Write-Status 'applying'
    foreach ($entry in $retirements) {
        [void](New-Item -ItemType Directory -Path (Split-Path -Parent $entry.Backup) -Force)
        $change = @{ Entry = $entry; BackupMoved = $false; Installed = $false }
        $changes.Add($change)
        Move-Item -LiteralPath $entry.Target -Destination $entry.Backup
        $change.BackupMoved = $true
    }
    # Retire old versions transactionally so importlib.metadata cannot report
    # an obsolete distribution after a successful resource-only upgrade.
    $siteRoot = Join-Path $packageRoot 'resources\python\Lib\site-packages'
    foreach ($old in Get-ChildItem -LiteralPath $siteRoot -Directory -Filter 'xuloom-*.dist-info') {
        if ($old.Name -eq $manifest.metadataDirectory) { continue }
        $entry = @{ Target = $old.FullName; Backup = (Join-Path $stageRoot ('backup\retired-metadata\' + $old.Name)) }
        Assert-Child $entry.Target $packageRoot
        Assert-Child $entry.Backup $stageRoot
        if (Test-Path -LiteralPath $entry.Backup) { throw 'Retired metadata backup already exists.' }
        [void](New-Item -ItemType Directory -Path (Split-Path -Parent $entry.Backup) -Force)
        $change = @{ Entry = $entry; BackupMoved = $false; Installed = $false }
        $changes.Add($change)
        Move-Item -LiteralPath $entry.Target -Destination $entry.Backup
        $change.BackupMoved = $true
    }
    foreach ($entry in $entries) {
        Assert-Child $entry.Target $packageRoot
        [void](New-Item -ItemType Directory -Path (Split-Path -Parent $entry.Backup) -Force)
        $change = @{ Entry = $entry; BackupMoved = $false; Installed = $false }
        $changes.Add($change)
        if (Test-Path -LiteralPath $entry.Target) {
            Move-Item -LiteralPath $entry.Target -Destination $entry.Backup
            $change.BackupMoved = $true
        }
        Move-Item -LiteralPath $entry.Source -Destination $entry.Target
        $change.Installed = $true
        foreach ($relative in $entry.Hashes.Keys) {
            $installedFile = Join-Path $entry.Target $relative
            Assert-Child $installedFile $packageRoot
            if ((Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash -ne $entry.Hashes[$relative]) { throw 'Updated file verification failed.' }
        }
    }
    Write-Status 'applied'
} catch {
    for ($index = $changes.Count - 1; $index -ge 0; $index--) {
        $change = $changes[$index]; $entry = $change.Entry
        if ($change.Installed) { Move-Item -LiteralPath $entry.Target -Destination $entry.Source }
        if ($change.BackupMoved) { Move-Item -LiteralPath $entry.Backup -Destination $entry.Target }
    }
    Write-Status 'failed' $_.Exception.Message
    exit 1
}
