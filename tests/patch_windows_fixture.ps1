param(
    [ValidateSet('setup', 'install', 'restore', 'installed', 'restored', 'tamper', 'long-path', 'reset-path', 'legacy-backup', 'install-locked', 'restore-locked', 'cleanup')]
    [string]$Phase,
    [string]$ZipPath,
    [string]$Root,
    [int]$RootLength = 65
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding = New-Object Text.UTF8Encoding($false)

# Only this test process uses the restrictive .NET mode. No registry changes.
$switchType = [object].Assembly.GetType('System.AppContextSwitches')
$flags = [Reflection.BindingFlags]'NonPublic,Static'
$switchType.GetField('_blockLongPaths', $flags).SetValue($null, 1)

function Hash([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose(); $stream.Dispose() }
}

if ($Phase -eq 'setup') {
    $temp = [IO.Path]::GetTempPath().TrimEnd('\')
    $suffixLength = $RootLength - $temp.Length - 5
    if ($suffixLength -lt 8 -or $suffixLength -gt 32) { throw 'Unsupported TEMP length for this fixture.' }
    $Root = Join-Path $temp ('toi-' + [Guid]::NewGuid().ToString('N').Substring(0, $suffixLength))
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($ZipPath, $Root)
    $patch = Join-Path $Root 'KoreanPatch'
    $manifest = Get-Content -LiteralPath (Join-Path $patch 'patch-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($row in $manifest.game_files) {
        $path = Join-Path $Root $row.path.Replace('/', '\')
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
        [IO.File]::WriteAllBytes($path, [Text.Encoding]::UTF8.GetBytes("original`n"))
    }
    [IO.File]::WriteAllText((Join-Path $Root 'Trials of Innocence.exe'), '')
    Write-Output $Root
    exit 0
}

# Every mutation below is restricted to the test's own Windows TEMP folder.
$temp = [IO.Path]::GetTempPath().TrimEnd('\')
if ([IO.Path]::GetDirectoryName($Root) -ne $temp -or [IO.Path]::GetFileName($Root) -notmatch '^toi-[0-9a-f]{8,32}$') {
    throw 'Not a fixture directory.'
}
if ($Phase -eq 'cleanup') { [IO.Directory]::Delete($Root, $true); exit 0 }
$patch = Join-Path $Root 'KoreanPatch'
$manifest = Get-Content -LiteralPath (Join-Path $patch 'patch-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$scriptPath = Join-Path $patch 'patch.ps1'

if ($Phase -in @('install', 'restore', 'install-locked', 'restore-locked')) {
    $action = $Phase.Split('-')[0]
    $lock = $null
    try {
        if ($Phase.EndsWith('-locked')) {
            $row = @($manifest.changes | Where-Object { $_.operation -eq 'replace' -and $_.path -notlike '*/catalog.json' })[10]
            $path = Join-Path $Root $row.path.Replace('/', '\')
            $lock = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        }
        & $scriptPath -Action $action
    } finally { if ($null -ne $lock) { $lock.Dispose() } }
    exit $LASTEXITCODE
}

if ($Phase -eq 'tamper') {
    $row = $manifest.changes[0]
    $relative = $row.path
    if ($row.PSObject.Properties.Name -contains 'payload_path') { $relative = $row.payload_path }
    [IO.File]::WriteAllText((Join-Path (Join-Path $patch 'payload') $relative.Replace('/', '\')), 'tampered')
    exit 0
}

if ($Phase -in @('long-path', 'reset-path')) {
    $manifestPath = Join-Path $patch 'patch-manifest.json'
    $saved = Join-Path $patch 'fixture-manifest.json'
    if ($Phase -eq 'long-path') {
        [IO.File]::Copy($manifestPath, $saved, $false)
        $manifest.game_files[-1].path = 'Trials of Innocence_Data/StreamingAssets/aa/' + ('x' * 160) + '/leaf.bundle'
        [IO.File]::WriteAllText($manifestPath, (ConvertTo-Json -InputObject $manifest -Depth 10), (New-Object Text.UTF8Encoding($false)))
    } else {
        [IO.File]::Copy($saved, $manifestPath, $true)
        [IO.File]::Delete($saved)
    }
    [IO.File]::WriteAllText((Join-Path $patch 'patch-manifest.sha256'), (Hash $manifestPath))
    exit 0
}

if ($Phase -eq 'legacy-backup') {
    $backup = Join-Path $patch 'backup'
    $recordPath = Join-Path $backup 'backup-manifest.json'
    $record = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($row in $manifest.changes) {
        if ($row.operation -eq 'add') { continue }
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $key = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($row.path)))).Replace('-', '').ToLowerInvariant() + '.bin' }
        finally { $sha.Dispose() }
        $destination = Join-Path $backup $row.path.Replace('/', '\')
        [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination))
        [IO.File]::Move((Join-Path $backup $key), $destination)
    }
    $record.format = 'toi-l10n/game-patch-backup-v1'
    [IO.File]::WriteAllText($recordPath, (ConvertTo-Json -InputObject $record -Depth 10), (New-Object Text.UTF8Encoding($false)))
    exit 0
}

$changed = @{}
foreach ($row in $manifest.changes) { $changed[$row.path] = $row }
foreach ($row in $manifest.game_files) {
    $expected = $row.sha256
    if ($Phase -eq 'installed' -and $changed.ContainsKey($row.path)) { $expected = $changed[$row.path].output_sha256 }
    if ((Hash (Join-Path $Root $row.path.Replace('/', '\'))) -ne $expected) { throw "Unexpected file: $($row.path)" }
}
foreach ($row in $manifest.changes) {
    if ($row.operation -ne 'add') { continue }
    $path = Join-Path $Root $row.path.Replace('/', '\')
    if ($Phase -eq 'installed') {
        if ((Hash $path) -ne $row.output_sha256) { throw 'Added file was not installed.' }
    } elseif ([IO.File]::Exists($path)) { throw 'Added file remains after restore.' }
}
$state = [IO.File]::Exists((Join-Path $patch 'state.json'))
if ($state -ne ($Phase -eq 'installed')) { throw 'Incorrect installation state.' }
if (@(Get-ChildItem -LiteralPath $Root -Recurse -Force -Filter '.ko-tmp-*').Count -ne 0) { throw 'Temporary replacement files remain.' }
Write-Output "Verified $Phase : 3875 original files, 459 changes."
