#Requires -Version 5.1
<# Installs a complete language tree using the game's custom-language interface. #>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$GamePath,
    [Parameter(Mandatory = $true)][string]$PackagePath,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')][string]$LanguageName = 'LLC_zh-CN'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'This installer supports Windows only.' }

function Assert-NoLinks([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    while ($null -ne $item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point rejected: $($item.FullName)" }
        $item = $item.Parent
    }
}
function Assert-Tree([string]$Path) {
    Assert-NoLinks $Path
    foreach ($item in Get-ChildItem -LiteralPath $Path -Force -Recurse) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point rejected: $($item.FullName)" }
    }
}
function Assert-GameStopped {
    if (Get-Process -Name 'LimbusCompany' -ErrorAction SilentlyContinue) { throw 'Close Limbus Company first.' }
}
$game = (Resolve-Path -LiteralPath $GamePath).ProviderPath.TrimEnd('\')
$package = (Resolve-Path -LiteralPath $PackagePath).ProviderPath.TrimEnd('\')
if (-not (Test-Path -LiteralPath $package -PathType Container)) { throw 'PackagePath must be a directory.' }
if (-not (Test-Path -LiteralPath (Join-Path $game 'LimbusCompany.exe') -PathType Leaf)) { throw 'LimbusCompany.exe not found.' }
$data = Join-Path $game 'LimbusCompany_Data'
if (-not (Test-Path -LiteralPath $data -PathType Container)) { throw 'LimbusCompany_Data not found.' }
Assert-NoLinks $data
Assert-Tree $package
Assert-GameStopped
$lang = Join-Path $data 'Lang'
$destination = Join-Path $lang $LanguageName
if (Test-Path -LiteralPath $lang) { Assert-NoLinks $lang }
if (Test-Path -LiteralPath $destination) {
    if (-not (Test-Path -LiteralPath $destination -PathType Container)) { throw 'Destination is not a directory.' }
    Assert-Tree $destination
}
if (($package + '\').StartsWith($destination + '\', [StringComparison]::OrdinalIgnoreCase) -or
    ($destination + '\').StartsWith($package + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Package and destination must not overlap.' }
$context = Join-Path $package 'Font\Context'
$title = Join-Path $package 'Font\Title'
# The upstream font archive intentionally ships an empty Title directory.
if (-not (Test-Path -LiteralPath $context -PathType Container)) { throw 'Missing Font\Context.' }
if (-not (Test-Path -LiteralPath $title -PathType Container)) { throw 'Missing Font\Title.' }
$fonts = @(Get-ChildItem -LiteralPath $context -File | Where-Object { $_.Extension -in '.ttf', '.otf' -and $_.Length -gt 0 })
if ($fonts.Count -eq 0) { throw 'Font\Context must contain a nonempty TTF/OTF font.' }
$files = @(Get-ChildItem -LiteralPath $package -Recurse -File -Force)
$jsonFiles = @($files | Where-Object Extension -eq '.json')
if ($jsonFiles.Count -eq 0) { throw 'Package contains no JSON.' }
foreach ($file in $files) {
    if ($file.Extension -notin '.json', '.ttf', '.otf', '.txt', '.md' -and $file.Name -notin 'LICENSE', 'NOTICE') { throw "Unsupported package file: $($file.Name)" }
}
foreach ($file in $jsonFiles) {
    $body = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($body)) { throw "Empty JSON: $($file.FullName)" }
    $null = ConvertFrom-Json -InputObject $body
}
Write-Host "Validated $($jsonFiles.Count) JSON files. Destination: $destination"
# One decision covers the transaction; -WhatIf never creates staging or backups.
if (-not $PSCmdlet.ShouldProcess($destination, 'Stage, verify, back up, and install language package')) { return }
$nonce = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$backupRoot = Join-Path $game 'LimbusTranslationBackups'
if (Test-Path -LiteralPath $backupRoot) { Assert-NoLinks $backupRoot }
$backup = Join-Path $backupRoot ($LanguageName + '-' + $nonce)
$stage = Join-Path $data ('.limbus-stage-' + $nonce)
$backedUp = $false
$installed = $false
try {
    Copy-Item -LiteralPath $package -Destination $stage -Recurse
    Assert-Tree $stage
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($package.Length + 1)
        $copied = Join-Path $stage $relative
        if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $copied -Algorithm SHA256).Hash) { throw "Copy verification failed: $relative" }
    }
    Assert-GameStopped
    if (-not (Test-Path -LiteralPath $lang)) { $null = New-Item -ItemType Directory -Path $lang }
    if (Test-Path -LiteralPath $destination) {
        if (-not (Test-Path -LiteralPath $backupRoot)) { $null = New-Item -ItemType Directory -Path $backupRoot }
        Move-Item -LiteralPath $destination -Destination $backup
        $backedUp = $true
    }
    Move-Item -LiteralPath $stage -Destination $destination
    $installed = $true
} catch {
    $failure = $_
    if ($backedUp -and -not (Test-Path -LiteralPath $destination)) {
        Move-Item -LiteralPath $backup -Destination $destination
        Write-Host 'Previous language package restored.'
    }
    throw $failure
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
}
if ($installed) {
    Write-Host "Installed: $destination"
    if ($backedUp) { Write-Host "Backup: $backup" }
    Write-Host "Start the game, select custom language '$LanguageName', and use English as the base language."
}
