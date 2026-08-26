[CmdletBinding()]
param(
    [string]$LockFile = (Join-Path $PSScriptRoot 'image-lock.json'),
    [string]$DestinationDirectory = (Join-Path $PSScriptRoot 'artifacts'),
    [switch]$Apply,
    [switch]$Execute,
    [switch]$DownloadOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-ExternalTool {
    param([string[]]$Names)
    foreach ($name in $Names) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($null -ne $command) { return $command.Source }
    }
    return $null
}

$lockPath = (Resolve-Path -LiteralPath $LockFile).Path
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
$archiveName = [string]$lock.archive.filename
$archiveUrl = [string]$lock.archive.url
$expectedArchiveHash = ([string]$lock.archive.sha256).ToLowerInvariant()
$archivePath = Join-Path $DestinationDirectory $archiveName
$imageName = $archiveName -replace '\.xz$', ''
$imagePath = Join-Path $DestinationDirectory $imageName

Write-Host 'Pinned TideTracker base image'
Write-Host "  Release: $($lock.release_date) $($lock.codename) $($lock.architecture)"
Write-Host "  URL:     $archiveUrl"
Write-Host "  SHA-256: $expectedArchiveHash"
Write-Host "  Archive: $archivePath"
if (-not $DownloadOnly) { Write-Host "  Image:   $imagePath" }

if (-not $Apply -or -not $Execute) {
    Write-Host ''
    Write-Host 'DRY RUN: no directory, download, or image was created.'
    Write-Host 'Re-run with -Apply -Execute to download, verify, and decompress the pinned archive.'
    exit 0
}

New-Item -ItemType Directory -Path $DestinationDirectory -Force | Out-Null

if (-not (Test-Path -LiteralPath $archivePath)) {
    $partialPath = "$archivePath.partial"
    Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
    try {
        Invoke-WebRequest -Uri $archiveUrl -OutFile $partialPath -UseBasicParsing
        Move-Item -LiteralPath $partialPath -Destination $archivePath
    }
    finally {
        Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
    }
}

$actualArchiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualArchiveHash -ne $expectedArchiveHash) {
    throw "Archive SHA-256 mismatch. Expected $expectedArchiveHash, found $actualArchiveHash. The archive was retained for investigation."
}
Write-Host 'Archive checksum verified.'

if ($DownloadOnly) { exit 0 }
if (Test-Path -LiteralPath $imagePath) {
    throw "Refusing to overwrite existing image: $imagePath"
}

$xz = Get-ExternalTool @('xz.exe', 'xz')
$sevenZip = Get-ExternalTool @('7z.exe', '7z')
if ($null -ne $xz) {
    & $xz --decompress --keep -- $archivePath
    if ($LASTEXITCODE -ne 0) { throw "xz failed with exit code $LASTEXITCODE" }
}
elseif ($null -ne $sevenZip) {
    & $sevenZip x '-y' "-o$DestinationDirectory" -- $archivePath
    if ($LASTEXITCODE -ne 0) { throw "7-Zip failed with exit code $LASTEXITCODE" }
}
else {
    throw 'No xz or 7z executable was found. Install one, then rerun with -Apply.'
}

if (-not (Test-Path -LiteralPath $imagePath)) {
    throw "Decompressor reported success but did not create $imagePath"
}

$imageHash = (Get-FileHash -LiteralPath $imagePath -Algorithm SHA256).Hash.ToLowerInvariant()
$sidecarPath = "$imagePath.sha256"
Set-Content -LiteralPath $sidecarPath -Encoding ascii -NoNewline -Value "$imageHash  $imageName`n"

$manifest = [ordered]@{
    schema_version = 1
    source_lock = $lockPath
    source_url = $archiveUrl
    archive_file = $archivePath
    archive_sha256 = $actualArchiveHash
    image_file = $imagePath
    image_sha256 = $imageHash
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath "$imagePath.manifest.json" -Encoding utf8
Write-Host "Prepared and hashed image: $imagePath"
