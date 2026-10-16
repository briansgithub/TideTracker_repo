#requires -Version 7.2

[CmdletBinding()]
param(
    [string]$OpenSslPath,
    [string]$ExpectedOpenSslSha256,
    [string]$SshKeygenPath,
    [string]$ExpectedSshKeygenSha256,
    [string]$ExpectedRuntimeManifestSha256,
    [switch]$Apply,
    [switch]$Execute,
    [string]$Confirmation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$KeystoreVersion = 'v1'
$RequiredConfirmation = 'CREATE-TIDETRACKER-KEYSTORE-v1'
$TestModeToken = 'ALLOW-DISPOSABLE-LOCALAPPDATA-OVERRIDE-v1'
$SystemSid = 'S-1-5-18'
$Ed25519DerPrefix = [byte[]](
    0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00
)

function Get-NormalizedFullPath {
    param([Parameter(Mandatory)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathFullyQualified($Path)) {
        throw 'Keystore and tool paths must be absolute.'
    }
    $fullPath = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.Equals($root, [StringComparison]::OrdinalIgnoreCase)) { return $fullPath }
    return $fullPath.TrimEnd([IO.Path]::DirectorySeparatorChar)
}

function Test-PathIsWithin {
    param(
        [Parameter(Mandatory)][string]$Candidate,
        [Parameter(Mandatory)][string]$Parent
    )

    $candidatePath = Get-NormalizedFullPath -Path $Candidate
    $parentPath = Get-NormalizedFullPath -Path $Parent
    if ($candidatePath.Equals($parentPath, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = "$parentPath$([IO.Path]::DirectorySeparatorChar)"
    return $candidatePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-PathIsNotDriveD {
    param([Parameter(Mandatory)][string]$Path)

    $fullPath = Get-NormalizedFullPath -Path $Path
    $root = [IO.Path]::GetPathRoot($fullPath)
    if ($null -eq $root -or $root.StartsWith('\\', [StringComparison]::Ordinal)) {
        throw 'UNC and unknown-root paths are not accepted.'
    }
    if ($root.Equals('D:\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Drive D: is reserved for the removable card and is never a keystore path.'
    }
}

function Assert-NoExistingReparsePoint {
    param([Parameter(Mandatory)][string]$Path)

    $current = [IO.DirectoryInfo]::new((Get-NormalizedFullPath -Path $Path))
    while ($null -ne $current) {
        if ($current.Exists -and (($current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "Refusing a path beneath reparse point: $($current.FullName)"
        }
        $current = $current.Parent
    }
}

function Test-PathEntryExists {
    param([Parameter(Mandatory)][string]$Path)

    $fullPath = Get-NormalizedFullPath -Path $Path
    $parent = [IO.Path]::GetDirectoryName($fullPath)
    if ([string]::IsNullOrEmpty($parent) -or -not [IO.Directory]::Exists($parent)) {
        return $false
    }
    foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($parent)) {
        if ((Get-NormalizedFullPath -Path $entry).Equals(
            $fullPath,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            return $true
        }
    }
    return $false
}

function Initialize-WindowsPathApi {
    if ('TideTracker.WindowsPathApi' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace TideTracker {
    public static class WindowsPathApi {
        [StructLayout(LayoutKind.Sequential)]
        public struct ByHandleFileInformation {
            public uint FileAttributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern SafeFileHandle CreateFile(
            string fileName, uint desiredAccess, uint shareMode, IntPtr securityAttributes,
            uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern uint GetFinalPathNameByHandle(
            SafeFileHandle handle, StringBuilder path, uint pathLength, uint flags);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetFileInformationByHandle(
            SafeFileHandle handle, out ByHandleFileInformation information);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern uint QueryDosDevice(
            string deviceName, StringBuilder targetPath, int maximumLength);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        public static extern uint GetDriveType(string rootPathName);
    }
}
'@
}

function Get-FinalPathFromHandle {
    param(
        [Parameter(Mandatory)][Microsoft.Win32.SafeHandles.SafeFileHandle]$Handle,
        [Parameter(Mandatory)][ValidateSet('Dos', 'Nt')][string]$Format
    )

    $flags = if ($Format -ceq 'Nt') { [uint32]2 } else { [uint32]0 }
    $capacity = 512
    while ($true) {
        $buffer = [Text.StringBuilder]::new($capacity)
        $length = [TideTracker.WindowsPathApi]::GetFinalPathNameByHandle(
            $Handle, $buffer, [uint32]$buffer.Capacity, $flags
        )
        if ($length -eq 0) {
            throw [ComponentModel.Win32Exception]::new(
                [Runtime.InteropServices.Marshal]::GetLastWin32Error(),
                'Could not resolve a final filesystem path from a retained handle.'
            )
        }
        if ($length -lt $buffer.Capacity) {
            $resolved = $buffer.ToString()
            if ($Format -ceq 'Dos') {
                if ($resolved.StartsWith('\\?\UNC\', [StringComparison]::OrdinalIgnoreCase)) {
                    $resolved = '\\' + $resolved.Substring(8)
                }
                elseif ($resolved.StartsWith('\\?\', [StringComparison]::Ordinal)) {
                    $resolved = $resolved.Substring(4)
                }
                return Get-NormalizedFullPath -Path $resolved
            }
            return $resolved.TrimEnd('\')
        }
        $capacity = [int]$length + 1
    }
}

function Get-HandleIdentity {
    param([Parameter(Mandatory)][Microsoft.Win32.SafeHandles.SafeFileHandle]$Handle)

    $information = [TideTracker.WindowsPathApi+ByHandleFileInformation]::new()
    if (-not [TideTracker.WindowsPathApi]::GetFileInformationByHandle(
        $Handle, [ref]$information
    )) {
        throw [ComponentModel.Win32Exception]::new(
            [Runtime.InteropServices.Marshal]::GetLastWin32Error(),
            'Could not read retained filesystem identity.'
        )
    }
    return [pscustomobject]@{
        FinalDosPath = Get-FinalPathFromHandle -Handle $Handle -Format Dos
        FinalNtPath = Get-FinalPathFromHandle -Handle $Handle -Format Nt
        VolumeSerial = '{0:x8}' -f $information.VolumeSerialNumber
        FileId = '{0:x8}{1:x8}' -f $information.FileIndexHigh, $information.FileIndexLow
    }
}

function Open-DirectoryIdentity {
    param(
        [Parameter(Mandatory)][string]$Path,
        [switch]$AllowDeleteShare,
        [switch]$AllowNonFixed
    )

    Assert-NoExistingReparsePoint -Path $Path
    $fullPath = Get-NormalizedFullPath -Path $Path
    $item = Get-Item -LiteralPath $fullPath -Force
    if (-not $item.PSIsContainer -or
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "Filesystem identity anchor is not a regular directory: $Path"
    }
    [uint32]$shareMode = 1 -bor 2
    if ($AllowDeleteShare) { $shareMode = $shareMode -bor 4 }
    $handle = [TideTracker.WindowsPathApi]::CreateFile(
        $fullPath, 0, $shareMode, [IntPtr]::Zero, 3, 0x02000000, [IntPtr]::Zero
    )
    if ($handle.IsInvalid) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        $handle.Dispose()
        throw [ComponentModel.Win32Exception]::new(
            $errorCode, "Could not retain filesystem identity: $Path"
        )
    }
    try {
        $identity = Get-HandleIdentity -Handle $handle
        $driveRoot = [IO.Path]::GetPathRoot($identity.FinalDosPath)
        if ([string]::IsNullOrWhiteSpace($driveRoot) -or
            (-not $AllowNonFixed -and
                [TideTracker.WindowsPathApi]::GetDriveType($driveRoot) -ne 3)) {
            throw 'The keystore must resolve onto a fixed local volume.'
        }
        return [pscustomobject]@{
            Path = $fullPath
            Handle = $handle
            FinalDosPath = $identity.FinalDosPath
            FinalNtPath = $identity.FinalNtPath
            VolumeSerial = $identity.VolumeSerial
            FileId = $identity.FileId
        }
    }
    catch {
        $handle.Dispose()
        throw
    }
}

function Open-PlannedDirectoryIdentity {
    param([Parameter(Mandatory)][string]$Path)

    $fullPath = Get-NormalizedFullPath -Path $Path
    Assert-NoExistingReparsePoint -Path $fullPath
    $segments = [Collections.Generic.Stack[string]]::new()
    $anchorPath = $fullPath
    while (-not [IO.Directory]::Exists($anchorPath)) {
        if ([IO.File]::Exists($anchorPath)) {
            throw "Planned directory path collides with a file: $anchorPath"
        }
        $leaf = [IO.Path]::GetFileName($anchorPath)
        $parent = [IO.Path]::GetDirectoryName($anchorPath)
        if ([string]::IsNullOrEmpty($leaf) -or [string]::IsNullOrEmpty($parent)) {
            throw "No existing directory can anchor filesystem identity: $Path"
        }
        $segments.Push($leaf)
        $anchorPath = $parent
    }
    $anchor = Open-DirectoryIdentity -Path $anchorPath
    try {
        $finalDosPath = $anchor.FinalDosPath
        $finalNtPath = $anchor.FinalNtPath
        while ($segments.Count -gt 0) {
            $leaf = $segments.Pop()
            $finalDosPath = Join-Path $finalDosPath $leaf
            $finalNtPath = "$($finalNtPath.TrimEnd('\'))\$leaf"
        }
        return [pscustomobject]@{
            Path = $fullPath
            Handle = $anchor.Handle
            AnchorFinalNtPath = $anchor.FinalNtPath
            AnchorVolumeSerial = $anchor.VolumeSerial
            AnchorFileId = $anchor.FileId
            FinalDosPath = Get-NormalizedFullPath -Path $finalDosPath
            FinalNtPath = $finalNtPath.TrimEnd('\')
            VolumeSerial = $anchor.VolumeSerial
        }
    }
    catch {
        $anchor.Handle.Dispose()
        throw
    }
}

function Test-NtPathIsWithin {
    param(
        [Parameter(Mandatory)][string]$Candidate,
        [Parameter(Mandatory)][string]$Parent
    )

    $candidatePath = $Candidate.TrimEnd('\')
    $parentPath = $Parent.TrimEnd('\')
    return $candidatePath.Equals($parentPath, [StringComparison]::OrdinalIgnoreCase) -or
        $candidatePath.StartsWith("$parentPath\", [StringComparison]::OrdinalIgnoreCase)
}

function Get-DosDeviceTarget {
    param([Parameter(Mandatory)][ValidatePattern('^[A-Za-z]:$')][string]$Drive)

    $device = $Drive.ToUpperInvariant()
    for ($depth = 0; $depth -lt 8; $depth++) {
        $buffer = [Text.StringBuilder]::new(32768)
        $length = [TideTracker.WindowsPathApi]::QueryDosDevice(
            $device, $buffer, $buffer.Capacity
        )
        if ($length -eq 0) {
            $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            if ($errorCode -eq 2) { return $null }
            throw [ComponentModel.Win32Exception]::new(
                $errorCode, "Could not resolve reserved drive device $Drive."
            )
        }
        $target = $buffer.ToString().Split([char]0)[0].TrimEnd('\')
        if ($target.StartsWith('\??\', [StringComparison]::Ordinal) -and
            $target.Length -ge 6 -and $target[5] -eq ':') {
            $targetDrive = $target.Substring(4, 2)
            $suffix = $target.Substring(6)
            $targetBuffer = [Text.StringBuilder]::new(32768)
            if ([TideTracker.WindowsPathApi]::QueryDosDevice(
                $targetDrive, $targetBuffer, $targetBuffer.Capacity
            ) -eq 0) {
                throw [ComponentModel.Win32Exception]::new(
                    [Runtime.InteropServices.Marshal]::GetLastWin32Error(),
                    "Could not resolve substituted reserved drive device $Drive."
                )
            }
            $target = $targetBuffer.ToString().Split([char]0)[0].TrimEnd('\') + $suffix
        }
        return $target.TrimEnd('\')
    }
    throw "Reserved drive device resolution exceeded its bound: $Drive"
}

function Assert-KeystoreParentIdentity {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][object]$ExpectedIdentity,
        [Parameter(Mandatory)][Security.Principal.SecurityIdentifier]$UserSid
    )

    Assert-NoExistingReparsePoint -Path $Path
    $retained = Get-HandleIdentity -Handle $ExpectedIdentity.Handle
    $current = Open-DirectoryIdentity -Path $Path -AllowDeleteShare
    try {
        foreach ($property in @('FinalDosPath', 'FinalNtPath', 'VolumeSerial', 'FileId')) {
            if ([string]$retained.$property -cne [string]$ExpectedIdentity.$property -or
                [string]$current.$property -cne [string]$ExpectedIdentity.$property) {
                throw 'The retained keystore-parent filesystem identity changed during generation.'
            }
        }
    }
    finally {
        $current.Handle.Dispose()
    }
    Assert-RestrictedAcl -Path $Path -UserSid $UserSid
}

function Get-BytesSha256 {
    param([Parameter(Mandatory)][byte[]]$Bytes)

    return (
        [BitConverter]::ToString([Security.Cryptography.SHA256]::HashData($Bytes)) -replace '-', ''
    ).ToLowerInvariant()
}

function Get-StreamSha256 {
    param([Parameter(Mandatory)][IO.Stream]$Stream)

    if (-not $Stream.CanRead -or -not $Stream.CanSeek) {
        throw 'Hash input must be a readable, seekable stream.'
    }
    $position = $Stream.Position
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $Stream.Position = 0
        return ([BitConverter]::ToString($sha.ComputeHash($Stream)) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $Stream.Position = $position
        $sha.Dispose()
    }
}

function Assert-Sha256 {
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Label
    )

    if ($Value -cnotmatch '^[0-9a-fA-F]{64}$') {
        throw "$Label must be an exact 64-character SHA-256 digest."
    }
}

function Open-LockedExecutable {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$ExpectedSha256,
        [Parameter(Mandatory)][string]$Label
    )

    Assert-Sha256 -Value $ExpectedSha256 -Label "$Label expected digest"
    Assert-PathIsNotDriveD -Path $Path
    Assert-NoExistingReparsePoint -Path $Path
    $fullPath = Get-NormalizedFullPath -Path $Path
    $item = Get-Item -LiteralPath $fullPath -Force
    if ($item.PSIsContainer -or
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "$Label must be a regular, non-reparse executable."
    }
    $stream = [IO.File]::Open(
        $fullPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $actualSha256 = Get-StreamSha256 -Stream $stream
        if ($actualSha256 -cne $ExpectedSha256.ToLowerInvariant()) {
            throw "$Label SHA-256 mismatch."
        }
        $fileIdentity = Get-HandleIdentity -Handle $stream.SafeFileHandle
        return [pscustomobject]@{
            Path = $fileIdentity.FinalDosPath
            FinalNtPath = $fileIdentity.FinalNtPath
            VolumeSerial = $fileIdentity.VolumeSerial
            FileId = $fileIdentity.FileId
            Sha256 = $actualSha256
            Size = [int64]$stream.Length
            FileVersion = [string]$item.VersionInfo.FileVersion
            Stream = $stream
        }
    }
    catch {
        $stream.Dispose()
        throw
    }
}

function Open-LockedRuntimeFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Label
    )

    Assert-PathIsNotDriveD -Path $Path
    Assert-NoExistingReparsePoint -Path $Path
    $fullPath = Get-NormalizedFullPath -Path $Path
    $item = Get-Item -LiteralPath $fullPath -Force
    if ($item.PSIsContainer -or
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "$Label must be a regular, non-reparse runtime file."
    }
    $stream = [IO.File]::Open(
        $fullPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $fileIdentity = Get-HandleIdentity -Handle $stream.SafeFileHandle
        return [pscustomobject]@{
            Path = $fileIdentity.FinalDosPath
            FinalNtPath = $fileIdentity.FinalNtPath
            VolumeSerial = $fileIdentity.VolumeSerial
            FileId = $fileIdentity.FileId
            Sha256 = Get-StreamSha256 -Stream $stream
            Size = [int64]$stream.Length
            Stream = $stream
        }
    }
    catch {
        $stream.Dispose()
        throw
    }
}

function Open-KeyRuntimeManifest {
    param(
        [Parameter(Mandatory)][object]$OpenSsl,
        [Parameter(Mandatory)][object]$SshKeygen
    )

    $opened = [Collections.Generic.List[object]]::new()
    try {
        $openSslDirectory = [IO.Path]::GetDirectoryName($OpenSsl.Path)
        $sshDirectory = [IO.Path]::GetDirectoryName($SshKeygen.Path)
        $systemDirectory = Get-NormalizedFullPath -Path (Join-Path $sshDirectory '..')
        foreach ($dependency in @(
            @{ Role = 'openssl_msys_crypto'; Path = Join-Path $openSslDirectory 'msys-crypto-3.dll' },
            @{ Role = 'openssl_msys_ssl'; Path = Join-Path $openSslDirectory 'msys-ssl-3.dll' },
            @{ Role = 'openssl_msys_runtime'; Path = Join-Path $openSslDirectory 'msys-2.0.dll' },
            @{ Role = 'ssh_keygen_libcrypto'; Path = Join-Path $systemDirectory 'libcrypto.dll' }
        )) {
            $opened.Add((Open-LockedRuntimeFile -Path $dependency.Path -Label $dependency.Role))
        }
        $files = @(
            [ordered]@{
                path = $OpenSsl.Path
                role = 'openssl_executable'
                sha256 = $OpenSsl.Sha256
                size = $OpenSsl.Size
            },
            [ordered]@{
                path = $opened[0].Path
                role = 'openssl_msys_crypto'
                sha256 = $opened[0].Sha256
                size = $opened[0].Size
            },
            [ordered]@{
                path = $opened[1].Path
                role = 'openssl_msys_ssl'
                sha256 = $opened[1].Sha256
                size = $opened[1].Size
            },
            [ordered]@{
                path = $opened[2].Path
                role = 'openssl_msys_runtime'
                sha256 = $opened[2].Sha256
                size = $opened[2].Size
            },
            [ordered]@{
                path = $SshKeygen.Path
                role = 'ssh_keygen_executable'
                sha256 = $SshKeygen.Sha256
                size = $SshKeygen.Size
            },
            [ordered]@{
                path = $opened[3].Path
                role = 'ssh_keygen_libcrypto'
                sha256 = $opened[3].Sha256
                size = $opened[3].Size
            }
        )
        $manifest = [ordered]@{
            files = $files
            os_dll_policy = 'Windows protected platform DLLs are the operating-system TCB.'
            schema_version = 1
        }
        $bytes = Get-SortedCompactJsonBytes -Value $manifest
        return [pscustomobject]@{
            Manifest = $manifest
            Bytes = $bytes
            Sha256 = Get-BytesSha256 -Bytes $bytes
            Locks = @($opened)
        }
    }
    catch {
        foreach ($entry in $opened) { $entry.Stream.Dispose() }
        throw
    }
}

function Invoke-LockedTool {
    param(
        [Parameter(Mandatory)][object]$Tool,
        [Parameter(Mandatory)][AllowEmptyString()][string[]]$Arguments,
        [int]$TimeoutSeconds = 60
    )

    if ((Get-StreamSha256 -Stream $Tool.Stream) -cne $Tool.Sha256) {
        throw 'A locked key-generation executable changed during this run.'
    }
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Tool.Path
    $toolDirectory = [IO.Path]::GetDirectoryName($Tool.Path)
    $systemDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::System)
    $startInfo.WorkingDirectory = $toolDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in $Arguments) {
        $startInfo.ArgumentList.Add($argument)
    }
    $startInfo.Environment['PATH'] = "$toolDirectory;$systemDirectory"
    foreach ($name in @('SSH_AUTH_SOCK', 'SSH_ASKPASS', 'SSH_SK_PROVIDER', 'DISPLAY')) {
        $null = $startInfo.Environment.Remove($name)
    }
    if ([IO.Path]::GetFileName($Tool.Path).StartsWith('openssl', [StringComparison]::OrdinalIgnoreCase)) {
        $startInfo.Environment['OPENSSL_CONF'] = 'NUL'
        $null = $startInfo.Environment.Remove('OPENSSL_MODULES')
        $null = $startInfo.Environment.Remove('OPENSSL_ENGINES')
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw 'A key-generation tool could not be started.'
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try { $process.Kill($true) } catch { }
            throw 'A key-generation tool exceeded its bounded execution time.'
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $null = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw 'A key-generation or validation tool failed; its output was suppressed.'
        }
        return $stdout
    }
    finally {
        $process.Dispose()
    }
}

function New-RestrictedSecurity {
    param(
        [Parameter(Mandatory)][Security.Principal.SecurityIdentifier]$UserSid,
        [switch]$Directory
    )

    if ($Directory) {
        $security = [Security.AccessControl.DirectorySecurity]::new()
        $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    else {
        $security = [Security.AccessControl.FileSecurity]::new()
        $inheritance = [Security.AccessControl.InheritanceFlags]::None
    }
    $security.SetAccessRuleProtection($true, $false)
    $systemIdentity = [Security.Principal.SecurityIdentifier]::new($SystemSid)
    foreach ($identity in @($UserSid, $systemIdentity)) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $identity,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $null = $security.AddAccessRule($rule)
    }
    return $security
}

function Set-RestrictedAcl {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][Security.Principal.SecurityIdentifier]$UserSid,
        [switch]$Directory
    )

    $security = New-RestrictedSecurity -UserSid $UserSid -Directory:$Directory
    if ($Directory) {
        [IO.FileSystemAclExtensions]::SetAccessControl(
            [IO.DirectoryInfo]::new($Path),
            [Security.AccessControl.DirectorySecurity]$security
        )
    }
    else {
        [IO.FileSystemAclExtensions]::SetAccessControl(
            [IO.FileInfo]::new($Path),
            [Security.AccessControl.FileSecurity]$security
        )
    }
}

function Assert-RestrictedAcl {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][Security.Principal.SecurityIdentifier]$UserSid
    )

    $acl = Get-Acl -LiteralPath $Path
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne $UserSid.Value) {
        throw "Keystore output is not owned by the current user: $Path"
    }
    if (-not $acl.AreAccessRulesProtected) {
        throw "ACL inheritance remains enabled: $Path"
    }
    $allowed = @($UserSid.Value, $SystemSid)
    $rights = @{}
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        $identity = $rule.IdentityReference.Value
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $identity -notin $allowed) {
            throw "ACL contains an authority other than the current user and SYSTEM: $Path"
        }
        if (-not $rights.ContainsKey($identity)) { $rights[$identity] = 0 }
        $rights[$identity] = [int]$rights[$identity] -bor [int]$rule.FileSystemRights
    }
    $fullControl = [int][Security.AccessControl.FileSystemRights]::FullControl
    foreach ($identity in $allowed) {
        if (-not $rights.ContainsKey($identity) -or
            (([int]$rights[$identity] -band $fullControl) -ne $fullControl)) {
            throw "ACL does not grant full control to required authority $identity`: $Path"
        }
    }
}

function New-RestrictedDirectory {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][Security.Principal.SecurityIdentifier]$UserSid
    )

    if (Test-PathEntryExists -Path $Path) {
        throw "Refusing to reuse existing keystore path: $Path"
    }
    $null = [IO.Directory]::CreateDirectory($Path)
    Set-RestrictedAcl -Path $Path -UserSid $UserSid -Directory
    Assert-RestrictedAcl -Path $Path -UserSid $UserSid
}

function Protect-AndFlushFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][Security.Principal.SecurityIdentifier]$UserSid
    )

    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or
        (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "Generated key output is not a regular file: $Path"
    }
    Set-RestrictedAcl -Path $Path -UserSid $UserSid
    Assert-RestrictedAcl -Path $Path -UserSid $UserSid
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::Read
    )
    try { $stream.Flush($true) } finally { $stream.Dispose() }
}

function Write-NewRestrictedFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][byte[]]$Bytes,
        [Parameter(Mandatory)][Security.Principal.SecurityIdentifier]$UserSid
    )

    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    Set-RestrictedAcl -Path $Path -UserSid $UserSid
    Assert-RestrictedAcl -Path $Path -UserSid $UserSid
}

function Test-BytesEqual {
    param(
        [Parameter(Mandatory)][byte[]]$First,
        [Parameter(Mandatory)][byte[]]$Second
    )

    if ($First.Length -ne $Second.Length) { return $false }
    [int]$difference = 0
    for ($index = 0; $index -lt $First.Length; $index++) {
        $difference = $difference -bor ($First[$index] -bxor $Second[$index])
    }
    return $difference -eq 0
}

function Get-OpenSshEd25519Identity {
    param(
        [Parameter(Mandatory)][byte[]]$Bytes,
        [Parameter(Mandatory)][string]$Label
    )

    try {
        $text = [Text.UTF8Encoding]::new($false, $true).GetString($Bytes).Trim()
    }
    catch {
        throw "$Label is not strict UTF-8."
    }
    if ($text.Contains("`r") -or $text.Contains("`n")) {
        throw "$Label must contain exactly one OpenSSH record."
    }
    $fields = @($text -split '[\t ]+', 3)
    if ($fields.Count -lt 2 -or $fields[0] -cne 'ssh-ed25519') {
        throw "$Label is not an OpenSSH Ed25519 public key."
    }
    try { [byte[]]$wire = [Convert]::FromBase64String($fields[1]) }
    catch { throw "$Label contains invalid base64 key material." }
    if ($wire.Length -ne 51) { throw "$Label has an invalid Ed25519 wire length." }
    [uint32]$typeLength =
        ([uint32]$wire[0] -shl 24) -bor ([uint32]$wire[1] -shl 16) -bor
        ([uint32]$wire[2] -shl 8) -bor [uint32]$wire[3]
    [uint32]$keyLength =
        ([uint32]$wire[15] -shl 24) -bor ([uint32]$wire[16] -shl 16) -bor
        ([uint32]$wire[17] -shl 8) -bor [uint32]$wire[18]
    if ($typeLength -ne 11 -or
        [Text.Encoding]::ASCII.GetString($wire, 4, 11) -cne 'ssh-ed25519' -or
        $keyLength -ne 32) {
        throw "$Label has invalid Ed25519 wire fields."
    }
    [byte[]]$der = $Ed25519DerPrefix + $wire[19..50]
    [byte[]]$fingerprint = [Security.Cryptography.SHA256]::HashData($wire)
    return [pscustomobject]@{
        RawSha256 = Get-BytesSha256 -Bytes $Bytes
        CanonicalSha256 = Get-BytesSha256 -Bytes $der
        FingerprintSha256 = "SHA256:$([Convert]::ToBase64String($fingerprint).TrimEnd('='))"
        Der = $der
    }
}

function Get-PemEd25519Identity {
    param(
        [Parameter(Mandatory)][byte[]]$Bytes,
        [Parameter(Mandatory)][string]$Label
    )

    try {
        $strictAscii = [Text.Encoding]::GetEncoding(
            20127,
            [Text.EncoderFallback]::ExceptionFallback,
            [Text.DecoderFallback]::ExceptionFallback
        )
        $lines = $strictAscii.GetString($Bytes).Trim().Split("`n")
    }
    catch { throw "$Label is not strict ASCII PEM." }
    $normalized = @($lines | ForEach-Object { $_.TrimEnd("`r") })
    if ($normalized.Count -lt 3 -or
        $normalized[0] -cne '-----BEGIN PUBLIC KEY-----' -or
        $normalized[-1] -cne '-----END PUBLIC KEY-----' -or
        @($normalized[1..($normalized.Count - 2)] | Where-Object {
            [string]::IsNullOrWhiteSpace($_) -or $_ -cne $_.Trim()
        }).Count -ne 0) {
        throw "$Label is not a strict public-key PEM."
    }
    try { [byte[]]$der = [Convert]::FromBase64String(($normalized[1..($normalized.Count - 2)] -join '')) }
    catch { throw "$Label contains invalid PEM base64 material." }
    if ($der.Length -ne 44 -or
        -not (Test-BytesEqual -First $der[0..11] -Second $Ed25519DerPrefix)) {
        throw "$Label is not an Ed25519 public key."
    }
    [byte[]]$fingerprint = [Security.Cryptography.SHA256]::HashData($der)
    return [pscustomobject]@{
        RawSha256 = Get-BytesSha256 -Bytes $Bytes
        CanonicalSha256 = Get-BytesSha256 -Bytes $der
        FingerprintSha256 = "SHA256:$([Convert]::ToBase64String($fingerprint).TrimEnd('='))"
        Der = $der
    }
}

function Get-SortedCompactJsonBytes {
    param([Parameter(Mandatory)][Collections.IDictionary]$Value)

    $sorted = [ordered]@{}
    foreach ($key in @($Value.Keys | Sort-Object)) { $sorted[$key] = $Value[$key] }
    $json = $sorted | ConvertTo-Json -Depth 12 -Compress
    return [Text.UTF8Encoding]::new($false).GetBytes($json)
}

function New-KeyPolicy {
    param([Parameter(Mandatory)][Collections.IDictionary]$Identities)

    $policyWithoutDigest = [ordered]@{ schema_version = 1 }
    foreach ($name in @(
        'workstation_to_controller_ssh',
        'controller_to_zero_ssh',
        'application_release',
        'lab_safety',
        'image_release'
    )) {
        $identity = $Identities[$name]
        $policyWithoutDigest["${name}_public_key_sha256"] = $identity.RawSha256
        $policyWithoutDigest["${name}_key_material_sha256"] = $identity.CanonicalSha256
    }
    $digest = Get-BytesSha256 -Bytes (Get-SortedCompactJsonBytes -Value $policyWithoutDigest)
    $policy = [ordered]@{}
    foreach ($key in $policyWithoutDigest.Keys) { $policy[$key] = $policyWithoutDigest[$key] }
    $policy['key_policy_sha256'] = $digest
    return $policy
}

function Assert-PrivatePemIsUnencryptedPkcs8 {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Label
    )

    $bytes = [IO.File]::ReadAllBytes($Path)
    try {
        $strictAscii = [Text.Encoding]::GetEncoding(
            20127,
            [Text.EncoderFallback]::ExceptionFallback,
            [Text.DecoderFallback]::ExceptionFallback
        )
        $text = $strictAscii.GetString($bytes).Trim()
    }
    catch { throw "$Label private key is not ASCII PEM." }
    if (-not $text.StartsWith("-----BEGIN PRIVATE KEY-----`n", [StringComparison]::Ordinal) -and
        -not $text.StartsWith("-----BEGIN PRIVATE KEY-----`r`n", [StringComparison]::Ordinal)) {
        throw "$Label private key is not unencrypted PKCS8 PEM."
    }
    if (-not $text.EndsWith('-----END PRIVATE KEY-----', [StringComparison]::Ordinal) -or
        $text.Contains('ENCRYPTED', [StringComparison]::Ordinal)) {
        throw "$Label private key is not unencrypted PKCS8 PEM."
    }
}

function Assert-TreeIsRestrictedAndRegular {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][Security.Principal.SecurityIdentifier]$UserSid
    )

    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        $path = $pending.Pop()
        $item = Get-Item -LiteralPath $path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Keystore output contains a reparse point: $path"
        }
        Assert-RestrictedAcl -Path $path -UserSid $UserSid
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $path -Force) {
                $pending.Push($child.FullName)
            }
        }
    }
}

function Remove-ValidatedStagingDirectory {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Parent
    )

    $fullPath = Get-NormalizedFullPath -Path $Path
    $fullParent = Get-NormalizedFullPath -Path $Parent
    if (-not (Test-PathIsWithin -Candidate $fullPath -Parent $fullParent) -or
        [IO.Path]::GetDirectoryName($fullPath) -cne $fullParent -or
        [IO.Path]::GetFileName($fullPath) -cnotmatch '^v1\.partial\.[0-9a-f]{32}$') {
        throw 'Refusing to clean a path that is not the exact generated staging sibling.'
    }
    if (-not [IO.Directory]::Exists($fullPath)) { return }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($fullPath)
    while ($pending.Count -gt 0) {
        $candidate = $pending.Pop()
        $item = Get-Item -LiteralPath $candidate -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Refusing to recursively clean a staging tree containing a reparse point.'
        }
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $candidate -Force) {
                $pending.Push($child.FullName)
            }
        }
    }
    [IO.Directory]::Delete($fullPath, $true)
}

if (-not $IsWindows) { throw 'TideTracker keystore creation is supported only on Windows.' }
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw 'LOCALAPPDATA is unavailable.'
}
Initialize-WindowsPathApi

$repositoryRoot = Get-NormalizedFullPath -Path (Split-Path -Parent $PSScriptRoot)
$knownLocalAppData = Get-NormalizedFullPath -Path (
    [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
)
$environmentLocalAppData = Get-NormalizedFullPath -Path $env:LOCALAPPDATA
Assert-PathIsNotDriveD -Path $environmentLocalAppData
$testModeValue = [string]$env:TIDETRACKER_KEYSTORE_TEST_MODE
if (-not [string]::IsNullOrEmpty($testModeValue) -and $testModeValue -cne $TestModeToken) {
    throw 'The disposable keystore test-mode token is invalid.'
}
$testMode = $testModeValue -ceq $TestModeToken
if (-not $environmentLocalAppData.Equals(
    $knownLocalAppData, [StringComparison]::OrdinalIgnoreCase
) -and -not $testMode) {
    throw 'LOCALAPPDATA does not match the Windows LocalApplicationData Known Folder.'
}
$localAppData = if ($testMode) { $environmentLocalAppData } else { $knownLocalAppData }
$keystoreParent = Join-Path $localAppData 'TideTracker\keystore'
$destination = Join-Path $keystoreParent $KeystoreVersion
Assert-PathIsNotDriveD -Path $destination
$repositoryIdentity = Open-DirectoryIdentity -Path $repositoryRoot
$destinationPlan = Open-PlannedDirectoryIdentity -Path $destination
$keystoreParentPlan = Open-PlannedDirectoryIdentity -Path $keystoreParent
if ((Test-PathIsWithin -Candidate $destination -Parent $repositoryRoot) -or
    (Test-NtPathIsWithin -Candidate $destinationPlan.FinalNtPath `
        -Parent $repositoryIdentity.FinalNtPath) -or
    ($destinationPlan.AnchorVolumeSerial -ceq $repositoryIdentity.VolumeSerial -and
        $destinationPlan.AnchorFileId -ceq $repositoryIdentity.FileId)) {
    throw 'The keystore may not be created inside the repository.'
}
if ($testMode) {
    $knownTempIdentity = Open-PlannedDirectoryIdentity -Path (Join-Path $knownLocalAppData 'Temp')
    try {
        if (-not (Test-NtPathIsWithin -Candidate $destinationPlan.FinalNtPath `
            -Parent $knownTempIdentity.FinalNtPath)) {
            throw 'The disposable LOCALAPPDATA override must resolve beneath the Windows user Temp folder.'
        }
    }
    finally {
        $knownTempIdentity.Handle.Dispose()
    }
}
$reservedDrive = $null
if ($testMode) {
    $candidateReservedDrive = [string]$env:TIDETRACKER_KEYSTORE_TEST_RESERVED_DRIVE
    if (-not [string]::IsNullOrWhiteSpace($candidateReservedDrive)) {
        if ($candidateReservedDrive -cnotmatch '^[A-Za-z]:$' -or
            $candidateReservedDrive.Equals('D:', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'The disposable reserved-drive override must be one non-D drive designator.'
        }
        $reservedDrive = $candidateReservedDrive.ToUpperInvariant()
    }
}
else {
    if (-not [string]::IsNullOrWhiteSpace(
        [string]$env:TIDETRACKER_KEYSTORE_TEST_RESERVED_DRIVE
    )) {
        throw 'The reserved-drive override is available only in disposable test mode.'
    }
    $reservedDrive = 'D:'
}
$reservedDevice = if ($null -ne $reservedDrive) {
    Get-DosDeviceTarget -Drive $reservedDrive
} else { $null }
$reservedVolumeIdentity = $null
if ($null -ne $reservedDevice) {
    $reservedVolumeIdentity = Open-DirectoryIdentity -Path "$reservedDrive\" `
        -AllowDeleteShare -AllowNonFixed
}
if ($null -ne $reservedDevice -and
    ((Test-NtPathIsWithin -Candidate $destinationPlan.FinalNtPath -Parent $reservedDevice) -or
        ($destinationPlan.AnchorVolumeSerial -ceq $reservedVolumeIdentity.VolumeSerial -and
            $destinationPlan.AnchorFileId -ceq $reservedVolumeIdentity.FileId))) {
    throw 'The keystore destination resolves onto the reserved removable-drive device.'
}
Assert-NoExistingReparsePoint -Path $destination
if (Test-PathEntryExists -Path $destination) {
    throw "Refusing to overwrite existing keystore: $destination"
}

Write-Host 'TideTracker guarded keystore plan'
Write-Host "  destination: $destination"
Write-Host '  identities:  2 OpenSSH Ed25519 + 3 PKCS8/SPKI Ed25519 PEM'
Write-Host '  private ACL: current Windows SID + SYSTEM only'
Write-Host '  overwrite:   forbidden'

$actionRequested = $Apply.IsPresent -or $Execute.IsPresent -or
    -not [string]::IsNullOrEmpty($Confirmation)
$toolArguments = @(
    @{ Value = $OpenSslPath; Label = 'OpenSslPath' },
    @{ Value = $ExpectedOpenSslSha256; Label = 'ExpectedOpenSslSha256' },
    @{ Value = $SshKeygenPath; Label = 'SshKeygenPath' },
    @{ Value = $ExpectedSshKeygenSha256; Label = 'ExpectedSshKeygenSha256' }
)
$toolArgumentCount = @($toolArguments | Where-Object {
    -not [string]::IsNullOrWhiteSpace([string]$_.Value)
}).Count
if (-not $actionRequested -and $toolArgumentCount -eq 0) {
    Write-Host 'DRY RUN: no directory or key was created.'
    Write-Host "Re-run with -Apply -Execute -Confirmation $RequiredConfirmation and both pinned tool paths/digests."
    exit 0
}
if ($actionRequested) {
    if (-not $Apply.IsPresent -or -not $Execute.IsPresent) {
        throw 'Creation requires both -Apply and -Execute.'
    }
    if ($Confirmation -cne $RequiredConfirmation) {
        throw "Confirmation must be exactly $RequiredConfirmation."
    }
}
foreach ($required in $toolArguments) {
    if ([string]::IsNullOrWhiteSpace([string]$required.Value)) {
        throw "$($required.Label) is required for runtime inspection and creation."
    }
}
if ($actionRequested) {
    Assert-Sha256 -Value $ExpectedRuntimeManifestSha256 `
        -Label 'ExpectedRuntimeManifestSha256'
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$userSid = $identity.User
if ($null -eq $userSid) { throw 'The current Windows SID is unavailable.' }
$openSsl = $null
$sshKeygen = $null
$runtime = $null
$staging = $null
$keystoreParentIdentity = $null
try {
    $openSsl = Open-LockedExecutable -Path $OpenSslPath `
        -ExpectedSha256 $ExpectedOpenSslSha256 -Label 'OpenSSL'
    $sshKeygen = Open-LockedExecutable -Path $SshKeygenPath `
        -ExpectedSha256 $ExpectedSshKeygenSha256 -Label 'ssh-keygen'
    $runtime = Open-KeyRuntimeManifest -OpenSsl $openSsl -SshKeygen $sshKeygen
    if ($null -ne $reservedDevice) {
        foreach ($runtimeFile in @($openSsl, $sshKeygen) + @($runtime.Locks)) {
            if (Test-NtPathIsWithin -Candidate $runtimeFile.FinalNtPath `
                -Parent $reservedDevice) {
                throw 'A key-generation executable or runtime dependency resolves onto the reserved removable-drive device.'
            }
        }
    }
    Write-Host "  runtime manifest SHA-256: $($runtime.Sha256)"
    Write-Host ([Text.UTF8Encoding]::new($false, $true).GetString($runtime.Bytes))
    if (-not $actionRequested) {
        Write-Host 'DRY RUN: runtime files were inspected read-only; no directory or key was created.'
        Write-Host "Accept the printed digest separately, then pass it as -ExpectedRuntimeManifestSha256 with $RequiredConfirmation."
        return
    }
    if ($runtime.Sha256 -cne $ExpectedRuntimeManifestSha256.ToLowerInvariant()) {
        throw 'Key-generation runtime manifest SHA-256 mismatch.'
    }

    $tideTrackerRoot = Join-Path $localAppData 'TideTracker'
    foreach ($parent in @($tideTrackerRoot, $keystoreParent)) {
        if ([IO.File]::Exists($parent)) { throw "Keystore parent is a file: $parent" }
        if (-not [IO.Directory]::Exists($parent)) {
            $null = [IO.Directory]::CreateDirectory($parent)
        }
        Assert-NoExistingReparsePoint -Path $parent
    }
    Set-RestrictedAcl -Path $keystoreParent -UserSid $userSid -Directory
    $keystoreParentIdentity = Open-DirectoryIdentity -Path $keystoreParent
    if ($keystoreParentIdentity.FinalNtPath -cne $keystoreParentPlan.FinalNtPath -or
        $keystoreParentIdentity.VolumeSerial -cne $keystoreParentPlan.VolumeSerial) {
        throw 'The created keystore parent does not match its retained planned filesystem identity.'
    }
    if ((Test-NtPathIsWithin -Candidate $keystoreParentIdentity.FinalNtPath `
        -Parent $repositoryIdentity.FinalNtPath) -or
        ($keystoreParentIdentity.VolumeSerial -ceq $repositoryIdentity.VolumeSerial -and
            $keystoreParentIdentity.FileId -ceq $repositoryIdentity.FileId)) {
        throw 'The created keystore parent resolves inside the repository.'
    }
    if ($null -ne $reservedVolumeIdentity -and
        ((Test-NtPathIsWithin -Candidate $keystoreParentIdentity.FinalNtPath `
            -Parent $reservedDevice) -or
            ($keystoreParentIdentity.VolumeSerial -ceq $reservedVolumeIdentity.VolumeSerial -and
                $keystoreParentIdentity.FileId -ceq $reservedVolumeIdentity.FileId))) {
        throw 'The created keystore parent resolves onto the reserved removable-drive device.'
    }
    Assert-KeystoreParentIdentity -Path $keystoreParent `
        -ExpectedIdentity $keystoreParentIdentity -UserSid $userSid
    if (Test-PathEntryExists -Path $destination) {
        throw "Refusing to overwrite existing keystore: $destination"
    }

    $staging = Join-Path $keystoreParent "v1.partial.$([Guid]::NewGuid().ToString('N'))"
    New-RestrictedDirectory -Path $staging -UserSid $userSid
    $privateRoot = Join-Path $staging 'private'
    $publicRoot = Join-Path $staging 'public'
    $recordsRoot = Join-Path $staging 'records'
    foreach ($directory in @($privateRoot, $publicRoot, $recordsRoot)) {
        New-RestrictedDirectory -Path $directory -UserSid $userSid
    }

    $identities = [ordered]@{}
    $publicRecords = [ordered]@{}
    foreach ($sshRole in @(
        @{ Name = 'workstation_to_controller_ssh'; File = 'workstation-to-controller-ssh-ed25519' },
        @{ Name = 'controller_to_zero_ssh'; File = 'controller-to-zero-ssh-ed25519' }
    )) {
        $privatePath = Join-Path $privateRoot $sshRole.File
        $generatedPublicPath = "$privatePath.pub"
        $publicName = "$($sshRole.File).pub"
        $publicPath = Join-Path $publicRoot $publicName
        Assert-KeystoreParentIdentity -Path $keystoreParent `
            -ExpectedIdentity $keystoreParentIdentity -UserSid $userSid
        $null = Invoke-LockedTool -Tool $sshKeygen -Arguments @(
            '-q', '-t', 'ed25519', '-N', '', '-C', "tidetracker-$KeystoreVersion-$($sshRole.File)",
            '-f', $privatePath
        )
        Protect-AndFlushFile -Path $privatePath -UserSid $userSid
        Protect-AndFlushFile -Path $generatedPublicPath -UserSid $userSid
        [IO.File]::Move($generatedPublicPath, $publicPath)
        Set-RestrictedAcl -Path $publicPath -UserSid $userSid
        $publicBytes = [IO.File]::ReadAllBytes($publicPath)
        $publicIdentity = Get-OpenSshEd25519Identity -Bytes $publicBytes -Label $sshRole.Name
        Assert-KeystoreParentIdentity -Path $keystoreParent `
            -ExpectedIdentity $keystoreParentIdentity -UserSid $userSid
        $derivedText = Invoke-LockedTool -Tool $sshKeygen -Arguments @('-y', '-f', $privatePath)
        $derivedIdentity = Get-OpenSshEd25519Identity `
            -Bytes ([Text.UTF8Encoding]::new($false).GetBytes($derivedText)) `
            -Label "$($sshRole.Name) derived public key"
        if (-not (Test-BytesEqual -First $publicIdentity.Der -Second $derivedIdentity.Der)) {
            throw "$($sshRole.Name) private key does not match its public key."
        }
        $identities[$sshRole.Name] = $publicIdentity
        $publicRecords[$sshRole.Name] = [ordered]@{
            algorithm = 'Ed25519'
            format = 'OpenSSH public key'
            public_file = "public/$publicName"
            public_key_sha256 = $publicIdentity.RawSha256
            key_material_sha256 = $publicIdentity.CanonicalSha256
            fingerprint_sha256 = $publicIdentity.FingerprintSha256
        }
    }

    foreach ($pemRole in @(
        @{ Name = 'application_release'; File = 'application-release-ed25519' },
        @{ Name = 'lab_safety'; File = 'lab-safety-approval-ed25519' },
        @{ Name = 'image_release'; File = 'image-release-ed25519' }
    )) {
        $privateName = "$($pemRole.File).private.pem"
        $publicName = "$($pemRole.File).public.pem"
        $privatePath = Join-Path $privateRoot $privateName
        $publicPath = Join-Path $publicRoot $publicName
        $derivedPath = Join-Path $recordsRoot ".$($pemRole.File).derived.public.pem"
        Assert-KeystoreParentIdentity -Path $keystoreParent `
            -ExpectedIdentity $keystoreParentIdentity -UserSid $userSid
        $null = Invoke-LockedTool -Tool $openSsl -Arguments @(
            'genpkey', '-algorithm', 'ED25519', '-out', $privatePath
        )
        Protect-AndFlushFile -Path $privatePath -UserSid $userSid
        Assert-PrivatePemIsUnencryptedPkcs8 -Path $privatePath -Label $pemRole.Name
        Assert-KeystoreParentIdentity -Path $keystoreParent `
            -ExpectedIdentity $keystoreParentIdentity -UserSid $userSid
        $null = Invoke-LockedTool -Tool $openSsl -Arguments @(
            'pkey', '-in', $privatePath, '-pubout', '-out', $publicPath
        )
        Protect-AndFlushFile -Path $publicPath -UserSid $userSid
        Assert-KeystoreParentIdentity -Path $keystoreParent `
            -ExpectedIdentity $keystoreParentIdentity -UserSid $userSid
        $null = Invoke-LockedTool -Tool $openSsl -Arguments @(
            'pkey', '-in', $privatePath, '-pubout', '-out', $derivedPath
        )
        Protect-AndFlushFile -Path $derivedPath -UserSid $userSid
        $publicIdentity = Get-PemEd25519Identity `
            -Bytes ([IO.File]::ReadAllBytes($publicPath)) -Label $pemRole.Name
        $derivedIdentity = Get-PemEd25519Identity `
            -Bytes ([IO.File]::ReadAllBytes($derivedPath)) -Label "$($pemRole.Name) derived public key"
        if (-not (Test-BytesEqual -First $publicIdentity.Der -Second $derivedIdentity.Der)) {
            throw "$($pemRole.Name) private key does not match its public key."
        }
        [IO.File]::Delete($derivedPath)
        $identities[$pemRole.Name] = $publicIdentity
        $publicRecords[$pemRole.Name] = [ordered]@{
            algorithm = 'Ed25519'
            format = 'SubjectPublicKeyInfo PEM'
            public_file = "public/$publicName"
            public_key_sha256 = $publicIdentity.RawSha256
            key_material_sha256 = $publicIdentity.CanonicalSha256
            fingerprint_sha256 = $publicIdentity.FingerprintSha256
        }
    }

    $canonicalHashes = @($identities.Values | ForEach-Object { $_.CanonicalSha256 })
    if (@($canonicalHashes | Sort-Object -Unique).Count -ne 5) {
        throw 'All five key identities must have distinct canonical Ed25519 material.'
    }
    $policy = New-KeyPolicy -Identities $identities
    $policyBytes = Get-SortedCompactJsonBytes -Value $policy
    Assert-KeystoreParentIdentity -Path $keystoreParent `
        -ExpectedIdentity $keystoreParentIdentity -UserSid $userSid
    Write-NewRestrictedFile -Path (Join-Path $recordsRoot 'key-policy.json') `
        -Bytes $policyBytes -UserSid $userSid

    $record = [ordered]@{
        schema_version = 1
        keystore_version = $KeystoreVersion
        created_utc = [DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
        current_user_sid = $userSid.Value
        tools = [ordered]@{
            openssl = [ordered]@{
                path = $openSsl.Path
                size = $openSsl.Size
                sha256 = $openSsl.Sha256
                file_version = $openSsl.FileVersion
            }
            ssh_keygen = [ordered]@{
                path = $sshKeygen.Path
                size = $sshKeygen.Size
                sha256 = $sshKeygen.Sha256
                file_version = $sshKeygen.FileVersion
            }
        }
        keys = $publicRecords
        key_policy = $policy
        runtime_manifest_sha256 = $runtime.Sha256
        runtime_manifest = $runtime.Manifest
    }
    $recordBytes = [Text.UTF8Encoding]::new($false).GetBytes(
        ($record | ConvertTo-Json -Depth 12 -Compress)
    )
    Assert-KeystoreParentIdentity -Path $keystoreParent `
        -ExpectedIdentity $keystoreParentIdentity -UserSid $userSid
    Write-NewRestrictedFile -Path (Join-Path $recordsRoot 'public-keystore-record.json') `
        -Bytes $recordBytes -UserSid $userSid

    Assert-TreeIsRestrictedAndRegular -Root $staging -UserSid $userSid
    Assert-KeystoreParentIdentity -Path $keystoreParent `
        -ExpectedIdentity $keystoreParentIdentity -UserSid $userSid
    Assert-NoExistingReparsePoint -Path $destination
    if (Test-PathEntryExists -Path $destination) {
        throw "Refusing to overwrite existing keystore: $destination"
    }
    if ((Get-StreamSha256 -Stream $openSsl.Stream) -cne $openSsl.Sha256 -or
        (Get-StreamSha256 -Stream $sshKeygen.Stream) -cne $sshKeygen.Sha256) {
        throw 'A pinned key-generation executable changed before publication.'
    }
    foreach ($runtimeFile in $runtime.Locks) {
        if ((Get-StreamSha256 -Stream $runtimeFile.Stream) -cne $runtimeFile.Sha256) {
            throw 'A pinned key-generation runtime dependency changed before publication.'
        }
    }
    [IO.Directory]::Move($staging, $destination)
    $staging = $null
    Assert-TreeIsRestrictedAndRegular -Root $destination -UserSid $userSid
    Write-Host "Created TideTracker keystore: $destination"
    Write-Host "Public key policy SHA-256: $($policy.key_policy_sha256)"
}
catch {
    $originalError = $_
    if ($null -ne $staging -and [IO.Directory]::Exists($staging)) {
        try {
            Remove-ValidatedStagingDirectory -Path $staging -Parent $keystoreParent
        }
        catch {
            throw "$($originalError.Exception.Message) Restricted staging cleanup also failed: $($_.Exception.Message)"
        }
    }
    throw $originalError
}
finally {
    if ($null -ne $runtime) {
        foreach ($runtimeFile in $runtime.Locks) { $runtimeFile.Stream.Dispose() }
    }
    if ($null -ne $openSsl) { $openSsl.Stream.Dispose() }
    if ($null -ne $sshKeygen) { $sshKeygen.Stream.Dispose() }
    if ($null -ne $keystoreParentIdentity) { $keystoreParentIdentity.Handle.Dispose() }
    if ($null -ne $reservedVolumeIdentity) { $reservedVolumeIdentity.Handle.Dispose() }
    $keystoreParentPlan.Handle.Dispose()
    $destinationPlan.Handle.Dispose()
    $repositoryIdentity.Handle.Dispose()
}
