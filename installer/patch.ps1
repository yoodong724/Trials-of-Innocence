param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('install', 'restore')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$PatchRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$GameRoot = [IO.Path]::GetFullPath((Join-Path $PatchRoot '..')).TrimEnd('\')
$ManifestPath = Join-Path $PatchRoot 'patch-manifest.json'
$ManifestHashPath = Join-Path $PatchRoot 'patch-manifest.sha256'
$BackupRoot = Join-Path $PatchRoot 'backup'
$StatePath = Join-Path $PatchRoot 'state.json'
$script:CheckedDirectories = @{}
$script:CompactBackup = $false
$script:PathKeys = @{}
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class KoreanPatchFileOps {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool MoveFileEx(string existingFile, string newFile, uint flags);
}
'@

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $hash.Dispose()
        $stream.Dispose()
    }
}

function Get-PathKey([string]$Relative) {
    if (-not $script:PathKeys.ContainsKey($Relative)) {
        $hash = [Security.Cryptography.SHA256]::Create()
        try {
            $bytes = [Text.Encoding]::UTF8.GetBytes($Relative)
            $script:PathKeys[$Relative] = ([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() + '.bin'
        } finally { $hash.Dispose() }
    }
    return $script:PathKeys[$Relative]
}

function Resolve-Payload([object]$Row) {
    $relative = $Row.path
    if ($Row.PSObject.Properties.Name -contains 'payload_path') {
        if ($Row.payload_path -ne (Get-PathKey $Row.path)) { throw "Invalid compact payload path: $($Row.path)" }
        $relative = $Row.payload_path
    }
    return Resolve-Under (Join-Path $PatchRoot 'payload') $relative
}

function Resolve-BackupFile([string]$Root, [object]$Row) {
    $relative = $Row.path
    if ($script:CompactBackup) { $relative = Get-PathKey $Row.path }
    return Resolve-Under $Root $relative
}

function Assert-NoReparse([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Reparse point is not allowed: $Path"
    }
}

function Assert-Parents([string]$Path, [string]$Root) {
    $directory = [IO.Path]::GetDirectoryName($Path)
    while ($directory.Length -ge $Root.Length) {
        if (-not $script:CheckedDirectories.ContainsKey($directory)) {
            if (Test-Path -LiteralPath $directory) { Assert-NoReparse $directory }
            $script:CheckedDirectories[$directory] = $true
        }
        if ($directory.Equals($Root, [StringComparison]::OrdinalIgnoreCase)) { break }
        $directory = [IO.Path]::GetDirectoryName($directory)
        if ([string]::IsNullOrEmpty($directory)) { throw 'Path has no trusted root.' }
    }
}

function Assert-PathLength([string]$Path) {
    if ($Path.Length -gt 259 -or ([IO.Path]::GetDirectoryName($Path)).Length -gt 247) {
        throw "Windows path limit exceeded ($($Path.Length) characters): $Path. Use a shorter Steam library path and extract the patch there."
    }
}

function Resolve-Under([string]$Root, [string]$Relative) {
    if ([string]::IsNullOrEmpty($Relative) -or $Relative.Contains('\') -or $Relative.StartsWith('/') -or $Relative.Contains(':')) {
        throw "Invalid relative path: $Relative"
    }
    foreach ($segment in $Relative.Split('/')) {
        if ([string]::IsNullOrEmpty($segment) -or $segment -eq '.' -or $segment -eq '..') {
            throw "Invalid path segment: $Relative"
        }
    }
    $path = $Root + '\' + $Relative.Replace('/', '\')
    Assert-PathLength $path
    $path = [IO.Path]::GetFullPath($path)
    if (-not $path.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes its root: $Relative"
    }
    Assert-Parents $path $Root
    if (Test-Path -LiteralPath $path) { Assert-NoReparse $path }
    return $path
}

function Assert-Hash([string]$Path, [string]$Expected, [string]$Label) {
    if (-not [IO.File]::Exists($Path)) { throw "Missing $Label`: $Path" }
    if ((Get-Sha256 $Path) -ne $Expected) { throw "SHA-256 mismatch for $Label`: $Path" }
}

function Replace-Atomic([string]$Source, [string]$Destination) {
    $temporary = Join-Path ([IO.Path]::GetDirectoryName($Destination)) ('.ko-tmp-' + [Guid]::NewGuid().ToString('N'))
    Assert-PathLength $temporary
    try {
        [IO.File]::Copy($Source, $temporary, $false)
        if (-not [KoreanPatchFileOps]::MoveFileEx($temporary, $Destination, 9)) {
            $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            throw "Atomic replace failed (Win32 $errorCode): $Destination"
        }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Write-JsonNew([string]$Path, [object]$Value) {
    $json = (ConvertTo-Json -InputObject $Value -Depth 10) + "`r`n"
    $temporary = $Path + '.tmp-' + [Guid]::NewGuid().ToString('N')
    Assert-PathLength $Path
    Assert-PathLength $temporary
    try {
        [IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
        if ([IO.File]::Exists($Path)) { throw "File already exists: $Path" }
        [IO.File]::Move($temporary, $Path)
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Assert-GameClosed {
    if (Get-Process -Name 'Trials of Innocence' -ErrorAction SilentlyContinue) {
        throw 'Close Trials of Innocence before installing or restoring the patch.'
    }
}

function Get-Manifest {
    Assert-NoReparse $PatchRoot
    if (-not [IO.File]::Exists((Join-Path $GameRoot 'Trials of Innocence.exe'))) {
        throw 'Put the KoreanPatch folder directly inside the Trials of Innocence game folder.'
    }
    Assert-NoReparse $GameRoot
    $expected = ([IO.File]::ReadAllText($ManifestHashPath)).Trim().ToLowerInvariant()
    if ($expected -notmatch '^[0-9a-f]{64}$') { throw 'Invalid manifest checksum file.' }
    Assert-Hash $ManifestPath $expected 'patch manifest'
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.format -ne 'toi-l10n/game-patch-v2' -or $manifest.game_files.Count -ne 3875 -or $manifest.changes.Count -lt 459 -or $manifest.changes.Count -gt 1100 -or $manifest.scope.changed_files -ne $manifest.changes.Count) {
        throw 'Unsupported or incomplete Korean patch manifest.'
    }
    $seen = @{}
    foreach ($row in $manifest.game_files) { [void](Resolve-Under $GameRoot $row.path) }
    $pending = Join-Path $PatchRoot ('backup.pending-' + ('0' * 32))
    foreach ($path in @($StatePath, (Join-Path $pending 'backup-manifest.json'))) {
        Assert-PathLength $path
        Assert-PathLength ($path + '.tmp-' + ('0' * 32))
    }
    foreach ($row in $manifest.changes) {
        if ($seen.ContainsKey($row.path)) { throw "Duplicate patch path: $($row.path)" }
        $seen[$row.path] = $true
        if ($row.path -notlike 'Trials of Innocence_Data/StreamingAssets/aa/*') {
            throw "Patch path is outside Addressables: $($row.path)"
        }
        if ($row.operation -notin @('replace', 'add') -or $row.output_sha256 -notmatch '^[0-9a-f]{64}$') {
            throw "Invalid patch file hash: $($row.path)"
        }
        if ($row.operation -eq 'replace' -and $row.source_sha256 -notmatch '^[0-9a-f]{64}$') {
            throw "Missing original hash for replacement: $($row.path)"
        }
        if ($row.operation -eq 'add' -and $null -ne $row.source_sha256) {
            throw "New file must not claim an original hash: $($row.path)"
        }
        $destination = Resolve-Under $GameRoot $row.path
        Assert-PathLength (Join-Path ([IO.Path]::GetDirectoryName($destination)) ('.ko-tmp-' + ('0' * 32)))
        [void](Resolve-Payload $row)
        if ($row.operation -eq 'replace') {
            $name = Get-PathKey $row.path
            Assert-PathLength (Join-Path $BackupRoot $name)
            Assert-PathLength (Join-Path $pending $name)
        }
    }
    return $manifest
}

function Assert-Payload([object]$Manifest) {
    foreach ($row in $Manifest.changes) {
        $source = Resolve-Payload $row
        Assert-Hash $source $row.output_sha256 'patch payload'
    }
}

function Assert-OriginalGame([object]$Manifest) {
    $seen = @{}
    $checked = 0
    foreach ($row in $Manifest.game_files) {
        if ($seen.ContainsKey($row.path)) { throw "Duplicate game inventory path: $($row.path)" }
        $seen[$row.path] = $true
        $path = Resolve-Under $GameRoot $row.path
        Assert-Hash $path $row.sha256 'game file'
        $checked++
        if ($checked % 1000 -eq 0) {
            Write-Output "  Game files checked: $checked/$($Manifest.game_files.Count)"
        }
    }
    foreach ($row in $Manifest.changes) {
        if ($row.operation -eq 'add' -and [IO.File]::Exists((Resolve-Under $GameRoot $row.path))) {
            throw "New patch path already exists in game: $($row.path)"
        }
    }
}

function Assert-Backup([object]$Manifest) {
    $recordPath = Join-Path $BackupRoot 'backup-manifest.json'
    if (-not [IO.File]::Exists($recordPath)) { throw 'Backup manifest is missing.' }
    $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($record.format -notin @('toi-l10n/game-patch-backup-v1', 'toi-l10n/game-patch-backup-v2') -or $record.patch_id -ne $Manifest.patch_id -or $record.files.Count -ne $Manifest.changes.Count) {
        throw 'Backup belongs to a different patch.'
    }
    $script:CompactBackup = $record.format -eq 'toi-l10n/game-patch-backup-v2'
    $byPath = @{}
    foreach ($row in $record.files) {
        if ($byPath.ContainsKey($row.path)) { throw "Duplicate backup path: $($row.path)" }
        $byPath[$row.path] = $row
    }
    foreach ($row in $Manifest.changes) {
        if (-not $byPath.ContainsKey($row.path) -or $byPath[$row.path].sha256 -ne $row.source_sha256) {
            throw "Backup entry mismatch: $($row.path)"
        }
        if ($row.operation -eq 'add') { continue }
        $path = Resolve-BackupFile $BackupRoot $row
        Assert-Hash $path $row.source_sha256 'backup file'
    }
}

function New-Backup([object]$Manifest) {
    if ([IO.Directory]::Exists($BackupRoot)) {
        Write-Output '[3/4] Checking existing backup...'
        Assert-Backup $Manifest
        return
    }
    Write-Output '[3/4] Backing up original files...'
    $script:CompactBackup = $true
    $pending = Join-Path $PatchRoot ('backup.pending-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($pending) | Out-Null
    try {
        $records = @()
        $backedUp = 0
        foreach ($row in $Manifest.changes) {
            if ($row.operation -eq 'replace') {
                $source = Resolve-Under $GameRoot $row.path
                $destination = Resolve-BackupFile $pending $row
                [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
                [IO.File]::Copy($source, $destination, $false)
                Assert-Hash $destination $row.source_sha256 'backup copy'
            }
            $records += [pscustomobject]@{ path = $row.path; sha256 = $row.source_sha256 }
            $backedUp++
            if ($backedUp % 150 -eq 0) {
                Write-Output "  Files backed up: $backedUp/$($Manifest.changes.Count)"
            }
        }
        $record = [pscustomobject]@{
            format = 'toi-l10n/game-patch-backup-v2'
            patch_id = $Manifest.patch_id
            created_at = [DateTime]::UtcNow.ToString('o')
            files = $records
        }
        Write-JsonNew (Join-Path $pending 'backup-manifest.json') $record
        [IO.Directory]::Move($pending, $BackupRoot)
    } finally {
        if ([IO.Directory]::Exists($pending)) { [IO.Directory]::Delete($pending, $true) }
    }
}

function Install-Patch([object]$Manifest) {
    Write-Output '[1/4] Verifying patch files...'
    Assert-Payload $Manifest
    if ([IO.File]::Exists($StatePath)) {
        Write-Output 'Checking installed files...'
        $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($state.patch_id -ne $Manifest.patch_id) { throw 'Another patch is recorded as installed.' }
        Assert-Backup $Manifest
        foreach ($row in $Manifest.changes) {
            Assert-Hash (Resolve-Under $GameRoot $row.path) $row.output_sha256 'installed file'
        }
        Write-Output 'Korean patch is already installed.'
        return
    }
    Write-Output '[2/4] Checking game version...'
    Assert-OriginalGame $Manifest
    New-Backup $Manifest
    $applied = New-Object System.Collections.ArrayList
    try {
        Write-Output '[4/4] Installing Korean files...'
        foreach ($row in $Manifest.changes) {
            $source = Resolve-Payload $row
            $destination = Resolve-Under $GameRoot $row.path
            if ($row.operation -eq 'add') {
                if ([IO.File]::Exists($destination)) { throw "New patch file unexpectedly exists: $($row.path)" }
                [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
            }
            Replace-Atomic $source $destination
            [void]$applied.Add($row)
            Assert-Hash $destination $row.output_sha256 'installed file'
            if ($applied.Count % 150 -eq 0) {
                Write-Output "  Files installed: $($applied.Count)/$($Manifest.changes.Count)"
            }
        }
        $state = [pscustomobject]@{
            format = 'toi-l10n/game-patch-state-v1'
            patch_id = $Manifest.patch_id
            installed_at = [DateTime]::UtcNow.ToString('o')
            changed_files = $Manifest.changes.Count
        }
        Write-JsonNew $StatePath $state
        Write-Output 'Korean patch installed. Select Korean in the game language menu.'
    } catch {
        $installError = $_
        $failures = @()
        for ($i = $applied.Count - 1; $i -ge 0; $i--) {
            $row = $applied[$i]
            try {
                $destination = Resolve-Under $GameRoot $row.path
                if ($row.operation -eq 'add') {
                    Assert-Hash $destination $row.output_sha256 'new patch rollback file'
                    [IO.File]::Delete($destination)
                } else {
                    Replace-Atomic (Resolve-BackupFile $BackupRoot $row) $destination
                    Assert-Hash $destination $row.source_sha256 'rollback file'
                }
            } catch {
                $failures += $row.path
            }
        }
        if ($failures.Count -gt 0) { throw "Install and rollback failed for: $($failures -join ', ')" }
        throw $installError
    }
}

function Restore-Patch([object]$Manifest) {
    Assert-Payload $Manifest
    if (-not [IO.Directory]::Exists($BackupRoot)) {
        throw 'No backup folder exists for this patch.'
    }
    Assert-Backup $Manifest
    $installed = @{}
    foreach ($row in $Manifest.changes) {
        $path = Resolve-Under $GameRoot $row.path
        if ($row.operation -eq 'add') {
            if ([IO.File]::Exists($path)) {
                Assert-Hash $path $row.output_sha256 'added patch file'
                $installed[$row.path] = $row.output_sha256
            } else {
                $installed[$row.path] = $null
            }
            continue
        }
        $hash = Get-Sha256 $path
        if ($hash -ne $row.source_sha256 -and $hash -ne $row.output_sha256) {
            throw "Modified game file blocks restore: $($row.path)"
        }
        $installed[$row.path] = $hash
    }
    $restored = New-Object System.Collections.ArrayList
    try {
        # The catalog is restored before its referenced bundles.
        $catalog = @($Manifest.changes | Where-Object { $_.path -like '*/catalog.json' })
        $bundles = @($Manifest.changes | Where-Object { $_.path -notlike '*/catalog.json' })
        foreach ($row in ($catalog + $bundles)) {
            if ($row.operation -eq 'add') {
                if ($null -eq $installed[$row.path]) { continue }
                [IO.File]::Delete((Resolve-Under $GameRoot $row.path))
                [void]$restored.Add($row)
            } else {
                if ($installed[$row.path] -eq $row.source_sha256) { continue }
                Replace-Atomic (Resolve-BackupFile $BackupRoot $row) (Resolve-Under $GameRoot $row.path)
                [void]$restored.Add($row)
                Assert-Hash (Resolve-Under $GameRoot $row.path) $row.source_sha256 'restored file'
            }
        }
        if ([IO.File]::Exists($StatePath)) { [IO.File]::Delete($StatePath) }
        Write-Output 'Original game files restored. Backup remains in KoreanPatch\\backup.'
    } catch {
        $restoreError = $_
        $failures = @()
        for ($i = $restored.Count - 1; $i -ge 0; $i--) {
            $row = $restored[$i]
            try {
                $destination = Resolve-Under $GameRoot $row.path
                if ($row.operation -eq 'add') { [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null }
                Replace-Atomic (Resolve-Payload $row) $destination
                Assert-Hash (Resolve-Under $GameRoot $row.path) $row.output_sha256 'restore rollback file'
            } catch {
                $failures += $row.path
            }
        }
        if ($failures.Count -gt 0) { throw "Restore and rollback failed for: $($failures -join ', ')" }
        throw $restoreError
    }
}

try {
    Assert-GameClosed
    $manifest = Get-Manifest
    if ($Action -eq 'install') {
        Install-Patch $manifest
    } else {
        Restore-Patch $manifest
    }
    exit 0
} catch {
    [Console]::Error.WriteLine("Korean patch $Action failed: $($_.Exception.Message)")
    exit 1
}
