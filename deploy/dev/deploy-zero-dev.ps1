[CmdletBinding()]
param(
    [string]$Target = '10.77.0.2',
    [string]$User = 'tidetracker',
    [string]$IdentityFile = (Join-Path $env:LOCALAPPDATA 'TideTracker\dev\ssh\zero-dev-ed25519'),
    [string]$BundleDirectory = (Join-Path $env:TEMP 'TideTracker-dev-bundles')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$bundleTool = Join-Path $PSScriptRoot 'dev_bundle.py'
if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    throw "Development SSH key is missing: $IdentityFile"
}

$python = Get-Command python -ErrorAction SilentlyContinue
$pythonArgs = @()
if ($null -eq $python) {
    $python = Get-Command py -ErrorAction Stop
    $pythonArgs = @('-3')
}
$ssh = (Get-Command ssh -ErrorAction Stop).Source
$scp = (Get-Command scp -ErrorAction Stop).Source

$null = New-Item -ItemType Directory -Force -Path $BundleDirectory
$bundle = Join-Path $BundleDirectory ("tidetracker-dev-{0}-{1}.tar" -f [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(), $PID)
$packJson = & $python.Source @pythonArgs $bundleTool pack --source $repo --output $bundle
if ($LASTEXITCODE -ne 0) { throw 'Development bundle creation failed.' }
$pack = $packJson | ConvertFrom-Json

$sshState = Join-Path $env:LOCALAPPDATA 'TideTracker\dev\ssh'
$null = New-Item -ItemType Directory -Force -Path $sshState
$knownHosts = Join-Path $sshState 'known_hosts'
$common = @(
    '-F', 'NUL',
    '-i', $IdentityFile,
    '-o', 'BatchMode=yes',
    '-o', 'IdentitiesOnly=yes',
    '-o', 'PreferredAuthentications=publickey',
    '-o', 'StrictHostKeyChecking=accept-new',
    '-o', "UserKnownHostsFile=$knownHosts",
    '-o', 'ConnectTimeout=10'
)

$remoteArchive = "/var/lib/tidetracker-dev/incoming/$($pack.release_id)-$($pack.archive_sha256).tar"
try {
    & $scp @common -- $bundle "${User}@${Target}:$remoteArchive"
    if ($LASTEXITCODE -ne 0) { throw 'Upload to the Pi Zero failed.' }

    $activate = "sudo -n /usr/local/sbin/tidetracker-dev-activate '$remoteArchive' '$($pack.archive_sha256)' '$($pack.release_id)' --remove-archive"
    & $ssh @common -- "${User}@${Target}" $activate
    if ($LASTEXITCODE -ne 0) {
        & $ssh @common -- "${User}@${Target}" "journalctl --no-pager -u tidetracker-dev.service -n 80"
        throw 'Pi Zero activation failed; the prior release was restored when available.'
    }
    & $ssh @common -- "${User}@${Target}" '/usr/local/bin/tidetracker-dev-status'
    if ($LASTEXITCODE -ne 0) { throw 'Activation succeeded but status retrieval failed.' }
}
finally {
    Remove-Item -LiteralPath $bundle -Force -ErrorAction SilentlyContinue
}
