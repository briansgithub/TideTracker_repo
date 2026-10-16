#requires -Version 7.2

[CmdletBinding()]
param(
    [string]$ImagePath,
    [int]$DiskNumber = -1,
    [string]$ExpectedSerial,
    [ValidateSet('controller', 'zero')]
    [string]$ExpectedRole,
    [string]$ExpectedImageSha256,
    [string]$ImageReleasePublicKey,
    [string]$ExpectedImageReleasePublicKeySha256,
    [string]$WorkstationToControllerSshPublicKey,
    [string]$ControllerToZeroSshPublicKey,
    [string]$ApplicationReleasePublicKey,
    [string]$LabSafetyPublicKey,
    [string]$OpenSslPath,
    [string]$ExpectedOpenSslSha256,
    [string]$Confirmation,
    [switch]$Apply,
    [switch]$Execute,
    [switch]$VerifyOnly,
    [switch]$OnlineAfterFlash,
    [switch]$AllowLargeDisk
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-StreamSha256 {
    param([System.IO.Stream]$Stream)
    if (-not $Stream.CanSeek -or -not $Stream.CanRead) {
        throw 'Hash input must be a seekable, readable stream.'
    }
    $originalPosition = $Stream.Position
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $Stream.Position = 0
        return ([BitConverter]::ToString($sha.ComputeHash($Stream)) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $Stream.Position = $originalPosition
        $sha.Dispose()
    }
}

function Open-LockedRegularFile {
    param(
        [string]$Path,
        [string]$Label
    )
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $item = Get-Item -LiteralPath $resolved -Force
    if ($item.PSIsContainer -or $null -ne $item.LinkType) {
        throw "$Label must be a regular, non-link file."
    }
    try {
        # FileShare.Read permits other readers but denies new or already-open
        # writers/deleters. Retaining this handle makes the hashed bytes the
        # exact bytes later consumed.
        $stream = [IO.File]::Open(
            $resolved,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
    }
    catch {
        throw "$Label cannot be locked against changes: $resolved"
    }
    return [pscustomobject]@{
        Path = $resolved
        Stream = $stream
        Length = [int64]$stream.Length
    }
}

function Read-LockedFileBytes {
    param(
        [string]$Path,
        [string]$Label,
        [int64]$MaximumBytes
    )
    $locked = Open-LockedRegularFile -Path $Path -Label $Label
    try {
        if ($locked.Length -lt 1 -or $locked.Length -gt $MaximumBytes) {
            throw "$Label is empty or exceeds its size limit."
        }
        $bytes = New-Object byte[] ([int]$locked.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $count = $locked.Stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($count -le 0) { throw "$Label ended before its declared length." }
            $offset += $count
        }
        return $bytes
    }
    finally {
        $locked.Stream.Dispose()
    }
}

function New-PrivateSnapshotFile {
    param(
        [byte[]]$Bytes,
        [string]$Suffix
    )
    $name = "tidetracker-flash-$([Guid]::NewGuid().ToString('N')).$Suffix"
    $path = Join-Path ([IO.Path]::GetTempPath()) $name
    $stream = [IO.File]::Open(
        $path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::Read
    )
    try {
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
        $stream.Position = 0
    }
    catch {
        $stream.Dispose()
        [IO.File]::Delete($path)
        throw
    }
    return [pscustomobject]@{
        Path = $path
        Stream = $stream
    }
}

function Get-BytesSha256 {
    param([byte[]]$Bytes)
    return (
        [BitConverter]::ToString([Security.Cryptography.SHA256]::HashData($Bytes)) `
            -replace '-', ''
    ).ToLowerInvariant()
}

function Get-OpenSshEd25519KeyIdentity {
    param(
        [byte[]]$Bytes,
        [string]$Label
    )
    try {
        $utf8 = [Text.UTF8Encoding]::new($false, $true)
        $line = $utf8.GetString($Bytes).Trim()
    }
    catch {
        throw "$Label is not strict UTF-8."
    }
    if ($line.Contains("`n") -or $line.Contains("`r")) {
        throw "$Label must contain exactly one OpenSSH public-key record."
    }
    $parts = @($line -split '[\t ]+', 3)
    if ($parts.Count -lt 2 -or $parts[0] -cne 'ssh-ed25519') {
        throw "$Label must be an OpenSSH Ed25519 public key."
    }
    try {
        [byte[]]$wire = [Convert]::FromBase64String($parts[1])
    }
    catch {
        throw "$Label has invalid OpenSSH base64 key material."
    }
    if ($wire.Length -ne 51) {
        throw "$Label has invalid Ed25519 wire-key length."
    }
    [uint32]$typeLength =
        ([uint32]$wire[0] -shl 24) -bor ([uint32]$wire[1] -shl 16) -bor `
        ([uint32]$wire[2] -shl 8) -bor [uint32]$wire[3]
    [uint32]$keyLength =
        ([uint32]$wire[15] -shl 24) -bor ([uint32]$wire[16] -shl 16) -bor `
        ([uint32]$wire[17] -shl 8) -bor [uint32]$wire[18]
    $type = [Text.Encoding]::ASCII.GetString($wire, 4, 11)
    if ($typeLength -ne 11 -or $type -cne 'ssh-ed25519' -or $keyLength -ne 32) {
        throw "$Label has invalid Ed25519 wire-key fields."
    }
    [byte[]]$derPrefix = 0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00
    [byte[]]$der = $derPrefix + $wire[19..50]
    return [pscustomobject]@{
        RawSha256 = Get-BytesSha256 -Bytes $Bytes
        CanonicalSha256 = Get-BytesSha256 -Bytes $der
    }
}

function Get-PemEd25519KeyIdentity {
    param(
        [byte[]]$Bytes,
        [string]$Label,
        [string]$OpenSsl
    )
    $snapshot = New-PrivateSnapshotFile -Bytes $Bytes -Suffix 'pub.pem'
    $derPath = Join-Path (
        [IO.Path]::GetTempPath()
    ) "tidetracker-flash-$([Guid]::NewGuid().ToString('N')).pub.der"
    try {
        & $OpenSsl pkey -pubin -in $snapshot.Path -outform DER -out $derPath 2>$null
        if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($derPath)) {
            throw "$Label must be an Ed25519 public PEM key."
        }
        [byte[]]$der = [IO.File]::ReadAllBytes($derPath)
        [byte[]]$expectedPrefix = 0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00
        if ($der.Length -ne 44) {
            throw "$Label must be an Ed25519 public PEM key."
        }
        for ($index = 0; $index -lt $expectedPrefix.Length; $index++) {
            if ($der[$index] -ne $expectedPrefix[$index]) {
                throw "$Label must be an Ed25519 public PEM key."
            }
        }
        return [pscustomobject]@{
            RawSha256 = Get-BytesSha256 -Bytes $Bytes
            CanonicalSha256 = Get-BytesSha256 -Bytes $der
        }
    }
    finally {
        $snapshot.Stream.Dispose()
        [IO.File]::Delete($snapshot.Path)
        [IO.File]::Delete($derPath)
    }
}

function Test-RoleImageKeySeparation {
    param(
        [byte[]]$ImageReleaseKeyBytes,
        [byte[]]$WorkstationToControllerSshKeyBytes,
        [byte[]]$ControllerToZeroSshKeyBytes,
        [byte[]]$ApplicationReleaseKeyBytes,
        [byte[]]$LabSafetyKeyBytes,
        [object]$Manifest,
        [string]$OpenSsl,
        [string]$ExpectedOpenSslHash
    )
    $lockedOpenSsl = Open-LockedRegularFile -Path $OpenSsl -Label 'OpenSSL executable'
    try {
        if ((Get-StreamSha256 -Stream $lockedOpenSsl.Stream) -cne $ExpectedOpenSslHash.ToLowerInvariant()) {
            throw 'OpenSSL executable changed during key-policy verification.'
        }
        $image = Get-PemEd25519KeyIdentity `
            -Bytes $ImageReleaseKeyBytes -Label 'Image release public key' -OpenSsl $lockedOpenSsl.Path
        $workstationSsh = Get-OpenSshEd25519KeyIdentity `
            -Bytes $WorkstationToControllerSshKeyBytes `
            -Label 'Workstation-to-controller SSH public key'
        $controllerSsh = Get-OpenSshEd25519KeyIdentity `
            -Bytes $ControllerToZeroSshKeyBytes `
            -Label 'Controller-to-Zero SSH public key'
        $application = Get-PemEd25519KeyIdentity `
            -Bytes $ApplicationReleaseKeyBytes -Label 'Application release public key' `
            -OpenSsl $lockedOpenSsl.Path
        $lab = Get-PemEd25519KeyIdentity `
            -Bytes $LabSafetyKeyBytes -Label 'Lab safety public key' -OpenSsl $lockedOpenSsl.Path
        $identities = [ordered]@{
            application_release = $application
            controller_to_zero_ssh = $controllerSsh
            image_release = $image
            lab_safety = $lab
            workstation_to_controller_ssh = $workstationSsh
        }
        foreach ($entry in $identities.GetEnumerator()) {
            $rawField = "$($entry.Key)_public_key_sha256"
            $canonicalField = "$($entry.Key)_key_material_sha256"
            if (
                $entry.Value.RawSha256 -cne [string]$Manifest.$rawField -or
                $entry.Value.CanonicalSha256 -cne [string]$Manifest.$canonicalField
            ) {
                throw "Key-policy input '$($entry.Key)' does not match the signed image manifest."
            }
        }
        $canonical = @(
            $image.CanonicalSha256,
            $workstationSsh.CanonicalSha256,
            $controllerSsh.CanonicalSha256,
            $application.CanonicalSha256,
            $lab.CanonicalSha256
        )
        if (@($canonical | Sort-Object -Unique).Count -ne $canonical.Count) {
            throw 'Both SSH, application-release, lab-safety, and image-release keys must all be separate.'
        }
        $policyRecord = [ordered]@{
            application_release_key_material_sha256 = $application.CanonicalSha256
            application_release_public_key_sha256 = $application.RawSha256
            controller_to_zero_ssh_key_material_sha256 = $controllerSsh.CanonicalSha256
            controller_to_zero_ssh_public_key_sha256 = $controllerSsh.RawSha256
            image_release_key_material_sha256 = $image.CanonicalSha256
            image_release_public_key_sha256 = $image.RawSha256
            lab_safety_key_material_sha256 = $lab.CanonicalSha256
            lab_safety_public_key_sha256 = $lab.RawSha256
            schema_version = 1
            workstation_to_controller_ssh_key_material_sha256 = $workstationSsh.CanonicalSha256
            workstation_to_controller_ssh_public_key_sha256 = $workstationSsh.RawSha256
        }
        $policyBytes = [Text.Encoding]::ASCII.GetBytes(
            ($policyRecord | ConvertTo-Json -Compress)
        )
        $policyHash = (
            [BitConverter]::ToString([Security.Cryptography.SHA256]::HashData($policyBytes)) `
                -replace '-', ''
        ).ToLowerInvariant()
        if ($policyHash -cne [string]$Manifest.key_policy_sha256) {
            throw 'Signed image manifest has an inconsistent canonical five-key policy digest.'
        }
        return [pscustomobject]@{
            ImageReleaseCanonicalSha256 = $image.CanonicalSha256
            WorkstationToControllerSshCanonicalSha256 = $workstationSsh.CanonicalSha256
            ControllerToZeroSshCanonicalSha256 = $controllerSsh.CanonicalSha256
            ApplicationReleaseCanonicalSha256 = $application.CanonicalSha256
            LabSafetyCanonicalSha256 = $lab.CanonicalSha256
            KeyPolicySha256 = $policyHash
        }
    }
    finally {
        $lockedOpenSsl.Stream.Dispose()
    }
}

function Assert-NoDuplicateJsonProperties {
    param(
        [System.Text.Json.JsonElement]$Element,
        [string]$JsonPath = '$'
    )
    if ($Element.ValueKind -eq [System.Text.Json.JsonValueKind]::Object) {
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($property in $Element.EnumerateObject()) {
            if (-not $names.Add($property.Name)) {
                throw "Role-image manifest repeats JSON property '$($property.Name)' at $JsonPath."
            }
            Assert-NoDuplicateJsonProperties -Element $property.Value -JsonPath "$JsonPath.$($property.Name)"
        }
    }
    elseif ($Element.ValueKind -eq [System.Text.Json.JsonValueKind]::Array) {
        $index = 0
        foreach ($child in $Element.EnumerateArray()) {
            Assert-NoDuplicateJsonProperties -Element $child -JsonPath "$JsonPath[$index]"
            $index++
        }
    }
}

function Test-RoleImageManifest {
    param(
        [byte[]]$ManifestBytes,
        [string]$Role,
        [string]$ActualImageHash
    )
    $keyIdentityPrefixes = @(
        'workstation_to_controller_ssh',
        'controller_to_zero_ssh',
        'application_release',
        'lab_safety',
        'image_release'
    )
    $requiredProperties = @(
        'schema_version',
        'role',
        'base_archive_sha256',
        'image_sha256',
        'image_build_id',
        'application_sha256',
        'package_bundle_sha256',
        'data_mib',
        'key_policy_sha256'
    )
    foreach ($prefix in $keyIdentityPrefixes) {
        $requiredProperties += "${prefix}_public_key_sha256"
        $requiredProperties += "${prefix}_key_material_sha256"
    }
    [int64]$schemaVersion = 0
    [int64]$dataMiB = 0
    [System.Text.Json.JsonElement]$schemaElement = [System.Text.Json.JsonElement]::new()
    try {
        $utf8 = [Text.UTF8Encoding]::new($false, $true)
        $manifestText = $utf8.GetString($ManifestBytes)
        $document = [System.Text.Json.JsonDocument]::Parse($manifestText)
        try {
            if ($document.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) {
                throw 'Role-image manifest root must be an object.'
            }
            Assert-NoDuplicateJsonProperties -Element $document.RootElement
            if (-not $document.RootElement.TryGetProperty('schema_version', [ref]$schemaElement)) {
                throw 'Role-image manifest is missing schema_version.'
            }
            if (
                $schemaElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Number -or
                -not $schemaElement.TryGetInt64([ref]$schemaVersion)
            ) {
                throw 'Role-image manifest schema_version must be the integer 2.'
            }
            if ($schemaVersion -eq 1) {
                throw 'Role-image manifest schema 1 is explicitly unsupported; rebuild as schema 2.'
            }
            if ($schemaVersion -ne 2) {
                throw 'Role-image manifest schema_version must be the integer 2.'
            }
            $actualProperties = @($document.RootElement.EnumerateObject() | ForEach-Object { $_.Name })
            $propertyDifference = @(
                Compare-Object `
                    -ReferenceObject $requiredProperties `
                    -DifferenceObject $actualProperties `
                    -CaseSensitive
            )
            if ($actualProperties.Count -ne $requiredProperties.Count -or $propertyDifference.Count -ne 0) {
                throw 'Role-image manifest properties do not match the exact schema-2 field set.'
            }
            $dataElement = $document.RootElement.GetProperty('data_mib')
            if (
                $dataElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Number -or
                -not $dataElement.TryGetInt64([ref]$dataMiB) -or
                $dataMiB -lt 512 -or
                $dataMiB -gt 8192
            ) {
                throw 'Role-image manifest data_mib must be an integer from 512 through 8192.'
            }
            if ($document.RootElement.GetProperty('role').ValueKind -ne [System.Text.Json.JsonValueKind]::String) {
                throw 'Role-image manifest role must be a string.'
            }
            foreach ($field in @(
                'base_archive_sha256',
                'image_sha256',
                'image_build_id',
                'application_sha256',
                'package_bundle_sha256',
                'key_policy_sha256'
            )) {
                if ($document.RootElement.GetProperty($field).ValueKind -ne [System.Text.Json.JsonValueKind]::String) {
                    throw "Role-image manifest field '$field' must be a string."
                }
            }
            foreach ($prefix in $keyIdentityPrefixes) {
                foreach ($suffix in @('public_key_sha256', 'key_material_sha256')) {
                    $field = "${prefix}_${suffix}"
                    if ($document.RootElement.GetProperty($field).ValueKind -ne [System.Text.Json.JsonValueKind]::String) {
                        throw "Role-image manifest field '$field' must be a string."
                    }
                }
            }
        }
        finally {
            $document.Dispose()
        }
        $manifest = $manifestText | ConvertFrom-Json
    }
    catch {
        throw "Role-image manifest is not strict, duplicate-free UTF-8 JSON: $($_.Exception.Message)"
    }
    if ([string]$manifest.role -cne $Role) {
        throw "Role-image manifest mismatch. Expected '$Role', found '$($manifest.role)'."
    }
    foreach ($field in @(
        'base_archive_sha256',
        'image_sha256',
        'image_build_id',
        'application_sha256',
        'package_bundle_sha256',
        'key_policy_sha256'
    )) {
        $value = [string]$manifest.$field
        if ($value -cnotmatch '^[0-9a-f]{64}$') {
            throw "Role-image manifest field '$field' is not a lowercase SHA-256 digest."
        }
    }
    foreach ($prefix in $keyIdentityPrefixes) {
        foreach ($suffix in @('public_key_sha256', 'key_material_sha256')) {
            $field = "${prefix}_${suffix}"
            $value = [string]$manifest.$field
            if ($value -cnotmatch '^[0-9a-f]{64}$') {
                throw "Role-image manifest field '$field' is not a lowercase SHA-256 digest."
            }
        }
    }
    if ([string]$manifest.image_sha256 -cne $ActualImageHash) {
        throw 'Role-image manifest image SHA-256 does not match the selected image.'
    }
    $buildRecord = [ordered]@{
        application_sha256 = [string]$manifest.application_sha256
        base_archive_sha256 = [string]$manifest.base_archive_sha256
        data_mib = $dataMiB
        key_policy_sha256 = [string]$manifest.key_policy_sha256
        package_bundle_sha256 = [string]$manifest.package_bundle_sha256
        role = [string]$manifest.role
        schema_version = $schemaVersion
    }
    $buildRecordBytes = [Text.Encoding]::UTF8.GetBytes(
        ($buildRecord | ConvertTo-Json -Compress)
    )
    $computedBuildId = (
        [BitConverter]::ToString([Security.Cryptography.SHA256]::HashData($buildRecordBytes)) `
            -replace '-', ''
    ).ToLowerInvariant()
    if ([string]$manifest.image_build_id -cne $computedBuildId) {
        throw 'Role-image manifest image_build_id is inconsistent with its signed build inputs.'
    }
    return $manifest
}

function Test-RoleImageSignature {
    param(
        [byte[]]$ManifestBytes,
        [byte[]]$SignatureBytes,
        [byte[]]$PublicKeyBytes,
        [string]$ExpectedPublicKeyHash,
        [string]$OpenSsl,
        [string]$ExpectedOpenSslHash
    )
    if ([string]::IsNullOrWhiteSpace($ExpectedPublicKeyHash) -or $ExpectedPublicKeyHash -cnotmatch '^[0-9a-fA-F]{64}$') {
        throw '-ExpectedImageReleasePublicKeySha256 must be the accepted 64-hex key digest.'
    }
    $actualKeyHash = ([BitConverter]::ToString([Security.Cryptography.SHA256]::HashData($PublicKeyBytes)) -replace '-', '').ToLowerInvariant()
    if ($actualKeyHash -cne $ExpectedPublicKeyHash.ToLowerInvariant()) {
        throw 'Image release public key does not match the accepted key digest.'
    }
    if ([string]::IsNullOrWhiteSpace($OpenSsl)) {
        throw '-OpenSslPath is required; PATH lookup is not accepted for release verification.'
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedOpenSslHash) -or $ExpectedOpenSslHash -cnotmatch '^[0-9a-fA-F]{64}$') {
        throw '-ExpectedOpenSslSha256 must be the accepted 64-hex executable digest.'
    }
    $lockedOpenSsl = Open-LockedRegularFile -Path $OpenSsl -Label 'OpenSSL executable'
    $snapshots = [Collections.Generic.List[object]]::new()
    try {
        $actualOpenSslHash = Get-StreamSha256 -Stream $lockedOpenSsl.Stream
        if ($actualOpenSslHash -cne $ExpectedOpenSslHash.ToLowerInvariant()) {
            throw 'OpenSSL executable does not match the accepted executable digest.'
        }
        $manifestSnapshot = New-PrivateSnapshotFile -Bytes $ManifestBytes -Suffix 'manifest.json'
        $snapshots.Add($manifestSnapshot)
        $signatureSnapshot = New-PrivateSnapshotFile -Bytes $SignatureBytes -Suffix 'manifest.sig'
        $snapshots.Add($signatureSnapshot)
        $keySnapshot = New-PrivateSnapshotFile -Bytes $PublicKeyBytes -Suffix 'pub.pem'
        $snapshots.Add($keySnapshot)
        & $lockedOpenSsl.Path pkey -pubin -in $keySnapshot.Path -text -noout 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Image release public key is invalid.' }
        & $lockedOpenSsl.Path pkeyutl -verify -pubin -inkey $keySnapshot.Path -rawin -in $manifestSnapshot.Path -sigfile $signatureSnapshot.Path 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Role-image manifest Ed25519 signature is invalid.' }
        return [pscustomobject]@{
            PublicKeySha256 = $actualKeyHash
            OpenSslSha256 = $actualOpenSslHash
        }
    }
    finally {
        $lockedOpenSsl.Stream.Dispose()
        foreach ($snapshot in $snapshots) {
            $snapshot.Stream.Dispose()
            [IO.File]::Delete($snapshot.Path)
        }
    }
}

function Test-RoleImageLayout {
    param(
        [System.IO.Stream]$ImageStream,
        [int64]$ImageLength,
        [int64]$DataMiB
    )
    if ($ImageLength -lt 1GB) { throw 'Refusing an image smaller than 1 GiB.' }
    if ($ImageLength % 512 -ne 0) { throw 'Image length is not an exact number of MBR sectors.' }
    if ($DataMiB -lt 512 -or $DataMiB -gt 8192) {
        throw 'Signed TIDEDATA size is outside the supported 512..8192 MiB range.'
    }
    $originalPosition = $ImageStream.Position
    try {
        $ImageStream.Position = 0
        $sector = New-Object byte[] 512
        if ($ImageStream.Read($sector, 0, $sector.Length) -ne $sector.Length) {
            throw 'Image does not contain a complete MBR sector.'
        }
    }
    finally {
        $ImageStream.Position = $originalPosition
    }
    if ($sector[510] -ne 0x55 -or $sector[511] -ne 0xAA) {
        throw 'Image does not have a valid MBR signature.'
    }
    $expectedTypes = @(0x0C, 0x83, 0x83)
    $partitions = @()
    for ($partition = 0; $partition -lt 4; $partition++) {
        $offset = 446 + ($partition * 16)
        $partitionType = $sector[$offset + 4]
        [uint64]$startSector = [BitConverter]::ToUInt32($sector, $offset + 8)
        [uint64]$sectorCount = [BitConverter]::ToUInt32($sector, $offset + 12)
        if ($partition -eq 3) {
            for ($entryByte = 0; $entryByte -lt 16; $entryByte++) {
                if ($sector[$offset + $entryByte] -ne 0) {
                    throw 'Image MBR partition entry 4 must be empty.'
                }
            }
            continue
        }
        if ($partitionType -eq 0 -or $startSector -eq 0 -or $sectorCount -eq 0) {
            throw "Image MBR partition $($partition + 1) is missing or empty."
        }
        if ($partitionType -ne $expectedTypes[$partition]) {
            throw "Image MBR partition $($partition + 1) has the wrong type."
        }
        [uint64]$endByte = ($startSector + $sectorCount) * 512
        if ($endByte -gt [uint64]$ImageLength) {
            throw "Image MBR partition $($partition + 1) extends beyond the image."
        }
        $partitions += [pscustomobject]@{ Start = $startSector; End = $startSector + $sectorCount }
    }
    for ($partition = 1; $partition -lt $partitions.Count; $partition++) {
        if ($partitions[$partition].Start -lt $partitions[$partition - 1].End) {
            throw 'The first three image MBR partitions overlap or are out of order.'
        }
    }
    [uint64]$expectedDataSectors = [uint64]$DataMiB * 2048
    if ([uint64]($partitions[2].End - $partitions[2].Start) -ne $expectedDataSectors) {
        throw 'Image MBR partition 3 size differs from signed data_mib.'
    }
    if ([uint64]$partitions[2].End -ne ([uint64]$ImageLength / 512)) {
        throw 'Image MBR partition 3 does not reach the exact image boundary.'
    }
}

function Assert-ImageSourceNotOnTarget {
    param(
        [string]$ResolvedPath,
        [int]$TargetDiskNumber
    )
    if ($ResolvedPath.StartsWith('\\') -and -not $ResolvedPath.StartsWith('\\?\Volume{')) {
        throw 'UNC image sources are not accepted because their backing disk cannot be proven.'
    }
    try {
        $sourceVolumes = @(Get-Volume -FilePath $ResolvedPath -ErrorAction Stop)
        if ($sourceVolumes.Count -ne 1) {
            throw 'Image source must resolve to exactly one local volume.'
        }
        $sourcePartitions = @(Get-Partition -Volume $sourceVolumes[0] -ErrorAction Stop)
    }
    catch {
        throw "Cannot prove the image source's backing disk: $($_.Exception.Message)"
    }
    if ($sourcePartitions.Count -ne 1) {
        throw 'Image source volume must resolve to exactly one local disk partition.'
    }
    if ([int]$sourcePartitions[0].DiskNumber -eq $TargetDiskNumber) {
        throw 'The image file resides on the selected target disk.'
    }
    try {
        $sourceDisk = Get-Disk -Number ([int]$sourcePartitions[0].DiskNumber) -ErrorAction Stop
    }
    catch {
        throw "Cannot prove the image source's physical disk: $($_.Exception.Message)"
    }
    if ([string]$sourceDisk.BusType -in @('Unknown', 'Virtual', 'File Backed Virtual', 'Spaces')) {
        throw 'Virtual, pooled, or unknown-backing image source disks are not accepted.'
    }
}

function Assert-DiskIdentity {
    param(
        [int]$Number,
        [string]$Serial,
        [string]$UniqueId,
        [uint64]$Size,
        [string]$BusType
    )
    $current = Get-Disk -Number $Number
    if (([string]$current.SerialNumber).Trim() -cne $Serial) {
        throw 'Target disk serial changed after validation.'
    }
    if ([string]$current.UniqueId -cne $UniqueId -or [uint64]$current.Size -ne $Size) {
        throw 'Target disk identity or size changed after validation.'
    }
    if ($current.IsBoot -or $current.IsSystem) { throw 'Target became a Windows boot or system disk.' }
    if ([string]$current.BusType -cne $BusType -or $current.BusType -notin @('USB', 'SD', 'MMC')) {
        throw 'Target disk bus type changed after validation.'
    }
    return $current
}

function Compare-ImageToDisk {
    param(
        [System.IO.Stream]$ImageStream,
        [int64]$ImageLength,
        [string]$PhysicalDrive
    )
    $bufferSize = 4MB
    $diskStream = [IO.File]::Open($PhysicalDrive, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $ImageStream.Position = 0
        $left = New-Object byte[] $bufferSize
        $right = New-Object byte[] $bufferSize
        [long]$offset = 0
        while (($leftCount = $ImageStream.Read($left, 0, $left.Length)) -gt 0) {
            $rightCount = 0
            while ($rightCount -lt $leftCount) {
                $read = $diskStream.Read($right, $rightCount, $leftCount - $rightCount)
                if ($read -le 0) { throw "Verification ended early at byte $($offset + $rightCount)" }
                $rightCount += $read
            }
            for ($index = 0; $index -lt $leftCount; $index++) {
                if ($left[$index] -ne $right[$index]) {
                    throw "Verification mismatch at byte $($offset + $index)"
                }
            }
            $offset += $leftCount
            Write-Progress -Activity 'Verifying flashed image' -Status "$offset of $ImageLength bytes" -PercentComplete (($offset * 100) / $ImageLength)
        }
        Write-Progress -Activity 'Verifying flashed image' -Completed
    }
    finally {
        $diskStream.Dispose()
    }
}

if ($Apply -and $VerifyOnly) { throw '-Apply and -VerifyOnly are mutually exclusive.' }
if ($Apply -and -not $Execute) { throw 'Flashing requires both -Apply and -Execute.' }
if ($Execute -and -not $Apply) { throw '-Execute is valid only together with -Apply.' }

if ($DiskNumber -lt 0) {
    Write-Host 'Candidate USB/SD/MMC disks (inspection only):'
    Get-Disk | Where-Object { $_.BusType -in @('USB', 'SD', 'MMC') } |
        Select-Object Number, FriendlyName, SerialNumber, BusType, Size, PartitionStyle, IsBoot, IsSystem |
        Format-Table -AutoSize
    Write-Host 'No disk was selected. Supply -DiskNumber, -ImagePath, and -ExpectedSerial for a guarded dry run.'
    exit 0
}

if ([string]::IsNullOrWhiteSpace($ImagePath)) { throw '-ImagePath is required when selecting a disk.' }
if ([string]::IsNullOrWhiteSpace($ExpectedRole)) { throw '-ExpectedRole controller|zero is required when selecting a disk.' }
$imageSource = Open-LockedRegularFile -Path $ImagePath -Label 'Image source'
try {
    $resolvedImage = $imageSource.Path
    if ([IO.Path]::GetExtension($resolvedImage) -cne '.img') {
        throw 'Only decompressed .img files are accepted.'
    }
    if ($imageSource.Length -lt 1GB) { throw 'Refusing an image smaller than 1 GiB.' }
    if ([string]::IsNullOrWhiteSpace($ExpectedImageSha256)) {
        throw '-ExpectedImageSha256 is required from the separately accepted release record; the adjacent manifest is not a trust anchor.'
    }
    $ExpectedImageSha256 = $ExpectedImageSha256.ToLowerInvariant()
    if ($ExpectedImageSha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Expected image SHA-256 must contain exactly 64 hexadecimal characters.'
    }
    if ([string]::IsNullOrWhiteSpace($ImageReleasePublicKey)) {
        throw '-ImageReleasePublicKey is required.'
    }
    foreach ($keyArgument in @(
        @{ Name = '-WorkstationToControllerSshPublicKey'; Value = $WorkstationToControllerSshPublicKey },
        @{ Name = '-ControllerToZeroSshPublicKey'; Value = $ControllerToZeroSshPublicKey },
        @{ Name = '-ApplicationReleasePublicKey'; Value = $ApplicationReleasePublicKey },
        @{ Name = '-LabSafetyPublicKey'; Value = $LabSafetyPublicKey }
    )) {
        if ([string]::IsNullOrWhiteSpace([string]$keyArgument.Value)) {
            throw "$($keyArgument.Name) is required for canonical key-separation verification."
        }
    }

    $disk = Get-Disk -Number $DiskNumber
    $serial = ([string]$disk.SerialNumber).Trim()
    $expectedSerialRaw = ([string]$ExpectedSerial).Trim()
    if ([string]::IsNullOrWhiteSpace($expectedSerialRaw)) {
        throw "-ExpectedSerial is required. Copy it from this script's inspection-only disk listing."
    }
    if ([string]::IsNullOrWhiteSpace($serial) -or $serial -cne $expectedSerialRaw) {
        throw "Disk serial mismatch. Expected '$expectedSerialRaw', found '$serial'."
    }
    if ($disk.IsBoot -or $disk.IsSystem) { throw 'Refusing a Windows boot or system disk.' }
    if ($disk.BusType -notin @('USB', 'SD', 'MMC')) { throw "Refusing bus type '$($disk.BusType)'." }
    if ($disk.Size -lt 4GB) { throw 'Refusing a disk smaller than 4 GiB.' }
    if (-not $AllowLargeDisk -and $disk.Size -gt 256GB) {
        throw 'Refusing a disk larger than 256 GB without -AllowLargeDisk.'
    }
    if ($imageSource.Length -gt $disk.Size) { throw 'The image is larger than the selected disk.' }
    Assert-ImageSourceNotOnTarget -ResolvedPath $resolvedImage -TargetDiskNumber $DiskNumber

    $actualImageHash = Get-StreamSha256 -Stream $imageSource.Stream
    if ($actualImageHash -cne $ExpectedImageSha256) {
        throw "Image SHA-256 mismatch. Expected $ExpectedImageSha256, found $actualImageHash."
    }
    $manifestPath = "$resolvedImage.manifest.json"
    $signaturePath = "$manifestPath.sig"
    $manifestBytes = Read-LockedFileBytes -Path $manifestPath -Label 'Role-image manifest' -MaximumBytes 1MB
    $signatureBytes = Read-LockedFileBytes -Path $signaturePath -Label 'Role-image manifest signature' -MaximumBytes 4KB
    $publicKeyBytes = Read-LockedFileBytes -Path $ImageReleasePublicKey -Label 'Image release public key' -MaximumBytes 64KB
    $workstationSshKeyBytes = Read-LockedFileBytes -Path $WorkstationToControllerSshPublicKey -Label 'Workstation-to-controller SSH public key' -MaximumBytes 64KB
    $controllerSshKeyBytes = Read-LockedFileBytes -Path $ControllerToZeroSshPublicKey -Label 'Controller-to-Zero SSH public key' -MaximumBytes 64KB
    $applicationReleaseKeyBytes = Read-LockedFileBytes -Path $ApplicationReleasePublicKey -Label 'Application release public key' -MaximumBytes 64KB
    $labSafetyKeyBytes = Read-LockedFileBytes -Path $LabSafetyPublicKey -Label 'Lab safety public key' -MaximumBytes 64KB
    $manifestHash = ([BitConverter]::ToString([Security.Cryptography.SHA256]::HashData($manifestBytes)) -replace '-', '').ToLowerInvariant()
    $manifestSignatureHash = ([BitConverter]::ToString([Security.Cryptography.SHA256]::HashData($signatureBytes)) -replace '-', '').ToLowerInvariant()
    $verificationIdentity = Test-RoleImageSignature `
        -ManifestBytes $manifestBytes `
        -SignatureBytes $signatureBytes `
        -PublicKeyBytes $publicKeyBytes `
        -ExpectedPublicKeyHash $ExpectedImageReleasePublicKeySha256 `
        -OpenSsl $OpenSslPath `
        -ExpectedOpenSslHash $ExpectedOpenSslSha256
    $roleManifest = Test-RoleImageManifest `
        -ManifestBytes $manifestBytes `
        -Role $ExpectedRole `
        -ActualImageHash $actualImageHash
    $keyPolicy = Test-RoleImageKeySeparation `
        -ImageReleaseKeyBytes $publicKeyBytes `
        -WorkstationToControllerSshKeyBytes $workstationSshKeyBytes `
        -ControllerToZeroSshKeyBytes $controllerSshKeyBytes `
        -ApplicationReleaseKeyBytes $applicationReleaseKeyBytes `
        -LabSafetyKeyBytes $labSafetyKeyBytes `
        -Manifest $roleManifest `
        -OpenSsl $OpenSslPath `
        -ExpectedOpenSslHash $ExpectedOpenSslSha256
    Test-RoleImageLayout `
        -ImageStream $imageSource.Stream `
        -ImageLength $imageSource.Length `
        -DataMiB ([int64]$roleManifest.data_mib)

    $identityText = [ordered]@{
        disk_number = $DiskNumber
        serial = $serial
        unique_id = [string]$disk.UniqueId
        size = [uint64]$disk.Size
        bus_type = [string]$disk.BusType
        role = $ExpectedRole
        image_sha256 = $actualImageHash
        image_build_id = [string]$roleManifest.image_build_id
        data_mib = [int64]$roleManifest.data_mib
        manifest_sha256 = $manifestHash
        manifest_signature_sha256 = $manifestSignatureHash
        image_release_public_key_sha256 = $verificationIdentity.PublicKeySha256
        image_release_key_material_sha256 = $keyPolicy.ImageReleaseCanonicalSha256
        workstation_to_controller_ssh_key_material_sha256 = $keyPolicy.WorkstationToControllerSshCanonicalSha256
        controller_to_zero_ssh_key_material_sha256 = $keyPolicy.ControllerToZeroSshCanonicalSha256
        application_release_key_material_sha256 = $keyPolicy.ApplicationReleaseCanonicalSha256
        lab_safety_key_material_sha256 = $keyPolicy.LabSafetyCanonicalSha256
        key_policy_sha256 = $keyPolicy.KeyPolicySha256
        openssl_sha256 = $verificationIdentity.OpenSslSha256
    } | ConvertTo-Json -Compress
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $identityBytes = [Text.Encoding]::UTF8.GetBytes($identityText)
        $identityHash = ([BitConverter]::ToString($sha.ComputeHash($identityBytes)) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
    $requiredConfirmation = "ERASE-DISK-$DiskNumber-$($identityHash.Substring(0, 16))"
    $physicalDrive = "\\.\PhysicalDrive$DiskNumber"

    Write-Host 'Guarded flash selection'
    Write-Host "  Target:       Disk $DiskNumber, $($disk.FriendlyName), $([math]::Round($disk.Size / 1GB, 2)) GB"
    Write-Host "  Serial:       $serial"
    Write-Host "  Image:        $resolvedImage"
    Write-Host "  Role:         $ExpectedRole"
    Write-Host "  Image SHA256: $actualImageHash"
    Write-Host "  Image build ID: $($roleManifest.image_build_id)"
    Write-Host "  TIDEDATA MiB: $($roleManifest.data_mib)"
    Write-Host "  Manifest SHA256: $manifestHash"
    Write-Host "  Manifest signature SHA256: $manifestSignatureHash"
    Write-Host "  Image signing key SHA256: $($verificationIdentity.PublicKeySha256)"
    Write-Host "  Canonical key separation: verified (both SSH/application/lab/image)"
    Write-Host "  OpenSSL SHA256: $($verificationIdentity.OpenSslSha256)"
    Write-Host "  Confirmation: $requiredConfirmation"

    if (-not $Apply -and -not $VerifyOnly) {
        Write-Host 'DRY RUN: no disk state or bytes were changed.'
        Write-Host "To flash, rerun with -Apply -Execute -Confirmation '$requiredConfirmation'."
        exit 0
    }
    if (-not (Test-IsAdministrator)) { throw 'Raw disk access requires an elevated PowerShell session.' }

    if ($VerifyOnly) {
        $null = Assert-DiskIdentity -Number $DiskNumber -Serial $serial -UniqueId ([string]$disk.UniqueId) -Size ([uint64]$disk.Size) -BusType ([string]$disk.BusType)
        Compare-ImageToDisk -ImageStream $imageSource.Stream -ImageLength $imageSource.Length -PhysicalDrive $physicalDrive
        if ((Get-StreamSha256 -Stream $imageSource.Stream) -cne $actualImageHash) {
            throw 'Locked image source no longer matches the accepted digest.'
        }
        $null = Assert-DiskIdentity -Number $DiskNumber -Serial $serial -UniqueId ([string]$disk.UniqueId) -Size ([uint64]$disk.Size) -BusType ([string]$disk.BusType)
        Write-Host 'Read-only verification succeeded.'
        exit 0
    }
    if ($Confirmation -cne $requiredConfirmation) {
        throw "Confirmation mismatch. Exact required value: $requiredConfirmation"
    }

    $disk = Assert-DiskIdentity -Number $DiskNumber -Serial $serial -UniqueId ([string]$disk.UniqueId) -Size ([uint64]$disk.Size) -BusType ([string]$disk.BusType)
    if ((Get-StreamSha256 -Stream $imageSource.Stream) -cne $actualImageHash) {
        throw 'Locked image source no longer matches the accepted digest.'
    }
    $writeAndVerifySucceeded = $false
    Set-Disk -Number $DiskNumber -IsOffline $true
    try {
        $null = Assert-DiskIdentity -Number $DiskNumber -Serial $serial -UniqueId ([string]$disk.UniqueId) -Size ([uint64]$disk.Size) -BusType ([string]$disk.BusType)
        $target = [IO.File]::Open($physicalDrive, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
        try {
            $imageSource.Stream.Position = 0
            $buffer = New-Object byte[] 4MB
            [long]$written = 0
            while (($count = $imageSource.Stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $target.Write($buffer, 0, $count)
                $written += $count
                Write-Progress -Activity 'Flashing TideTracker image' -Status "$written of $($imageSource.Length) bytes" -PercentComplete (($written * 100) / $imageSource.Length)
            }
            if ($written -ne $imageSource.Length) {
                throw "Image write length mismatch: wrote $written of $($imageSource.Length) bytes."
            }
            $target.Flush($true)
            Write-Progress -Activity 'Flashing TideTracker image' -Completed
        }
        finally {
            $target.Dispose()
        }

        Compare-ImageToDisk -ImageStream $imageSource.Stream -ImageLength $imageSource.Length -PhysicalDrive $physicalDrive
        if ((Get-StreamSha256 -Stream $imageSource.Stream) -cne $actualImageHash) {
            throw 'Locked image source no longer matches the accepted digest after verification.'
        }
        $null = Assert-DiskIdentity -Number $DiskNumber -Serial $serial -UniqueId ([string]$disk.UniqueId) -Size ([uint64]$disk.Size) -BusType ([string]$disk.BusType)
        $writeAndVerifySucceeded = $true
    }
    finally {
        if ($OnlineAfterFlash -and $writeAndVerifySucceeded) {
            $null = Assert-DiskIdentity -Number $DiskNumber -Serial $serial -UniqueId ([string]$disk.UniqueId) -Size ([uint64]$disk.Size) -BusType ([string]$disk.BusType)
            Set-Disk -Number $DiskNumber -IsOffline $false
        }
    }

    Write-Host 'Flash completed successfully.'
    if (-not $OnlineAfterFlash) {
        Write-Host 'The target remains offline intentionally; remove it safely before reinsertion.'
    }
}
finally {
    $imageSource.Stream.Dispose()
}
