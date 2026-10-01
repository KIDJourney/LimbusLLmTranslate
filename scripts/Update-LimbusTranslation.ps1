#Requires -Version 5.1
<# Updater for Limbus Company Chinese Translation #>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Continuous
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'This installer supports Windows only.' }

function Find-LimbusGamePath {
    $candidates = New-Object System.Collections.Generic.List[string]
    
    # Method 1: Check registry for Steam uninstaller (64-bit and 32-bit OS)
    $uninstallKeys = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 1973530",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 1973530"
    )
    foreach ($key in $uninstallKeys) {
        if (Test-Path $key) {
            $props = Get-ItemProperty $key -ErrorAction SilentlyContinue
            if ($null -ne $props -and $props.PSObject.Properties.Match('InstallLocation').Count -gt 0) {
                if (-not [string]::IsNullOrWhiteSpace($props.InstallLocation)) { 
                    $candidates.Add($props.InstallLocation)
                }
            }
        }
    }

    # Method 2: Check Steam library folders
    $steamKey = "HKCU:\Software\Valve\Steam"
    if (Test-Path $steamKey) {
        $props = Get-ItemProperty $steamKey -ErrorAction SilentlyContinue
        if ($null -ne $props -and $props.PSObject.Properties.Match('SteamPath').Count -gt 0) {
            if (-not [string]::IsNullOrWhiteSpace($props.SteamPath)) {
                $steamPath = $props.SteamPath
                $libraryFoldersPath = Join-Path $steamPath "steamapps\libraryfolders.vdf"
                $libraries = New-Object System.Collections.Generic.List[string]
                $libraries.Add((Join-Path $steamPath "steamapps"))
                
                if (Test-Path $libraryFoldersPath) {
                    $content = Get-Content $libraryFoldersPath
                    foreach ($line in $content) {
                        if ($line -match '"path"\s+"([^"]+)"') {
                            $basePath = $matches[1] -replace '\\\\', '\'
                            $libraries.Add((Join-Path $basePath "steamapps"))
                        }
                    }
                }
                
                foreach ($lib in $libraries) {
                    $manifestPath = Join-Path $lib "appmanifest_1973530.acf"
                    if (Test-Path $manifestPath) {
                        $manifest = Get-Content $manifestPath
                        $installDir = "Limbus Company"
                        foreach ($line in $manifest) {
                            if ($line -match '"installdir"\s+"([^"]+)"') {
                                $installDir = $matches[1]
                            }
                        }
                        $candidates.Add((Join-Path $lib "common\$installDir"))
                    }
                }
            }
        }
    }

    $validPaths = New-Object System.Collections.Generic.List[string]
    foreach ($cand in $candidates) {
        if ((Test-Path (Join-Path $cand "LimbusCompany.exe")) -and (Test-Path (Join-Path $cand "LimbusCompany_Data"))) {
            if (-not $validPaths.Contains($cand)) {
                $validPaths.Add($cand)
            }
        }
    }

    return $validPaths.ToArray()
}

function Assert-NoLinks([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    while ($null -ne $item) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point rejected: $($item.FullName)" }
        if ($item -is [System.IO.FileInfo]) {
            $item = $item.Directory
        } elseif ($item -is [System.IO.DirectoryInfo]) {
            $item = $item.Parent
        } else {
            $item = $null
        }
    }
}

function Invoke-UpdateCheck {
    Write-Host "Limbus Company Chinese Translation Updater" -ForegroundColor Cyan

    $appDataPath = Join-Path $env:LOCALAPPDATA "LimbusTranslationUpdater"
    $lastGamePathFile = Join-Path $appDataPath "last_game_path.txt"
    $gamePath = $null

    if (Test-Path $lastGamePathFile) {
        $savedPath = Get-Content $lastGamePathFile | Out-String | ForEach-Object Trim
        if ((Test-Path (Join-Path $savedPath "LimbusCompany.exe")) -and (Test-Path (Join-Path $savedPath "LimbusCompany_Data"))) {
            $gamePath = $savedPath
            Write-Host "Found previously used Game Path: $gamePath" -ForegroundColor Green
        }
    }

    if ($null -eq $gamePath) {
        $gamePaths = @(Find-LimbusGamePath)
        
        if ($gamePaths.Count -eq 0) {
            Write-Host "Auto-detection failed. Could not find Limbus Company." -ForegroundColor Yellow
            $gamePath = Read-Host "Please enter the path to the Limbus Company folder"
            if (-not (Test-Path (Join-Path $gamePath "LimbusCompany.exe"))) {
                Write-Host "Error: LimbusCompany.exe not found in $gamePath" -ForegroundColor Red
                exit 1
            }
        } elseif ($gamePaths.Count -eq 1) {
            $gamePath = $gamePaths[0]
            Write-Host "Found Game Path: $gamePath" -ForegroundColor Green
        } else {
            Write-Host "Multiple game installations found:" -ForegroundColor Yellow
            for ($i=0; $i -lt $gamePaths.Count; $i++) {
                Write-Host "[$i] $($gamePaths[$i])"
            }
            $selection = Read-Host "Please enter the number of the installation to update"
            if ($selection -lt 0 -or $selection -ge $gamePaths.Count) { throw "Invalid selection." }
            $gamePath = $gamePaths[[int]$selection]
        }
    }
    
    $gamePath = (Resolve-Path -LiteralPath $gamePath).ProviderPath.TrimEnd('\').ToLowerInvariant()
    $gamePathHash = (Get-FileHash -InputStream ([System.IO.MemoryStream]::new([System.Text.Encoding]::UTF8.GetBytes($gamePath))) -Algorithm MD5).Hash
    $localVersionFile = Join-Path $appDataPath "version_$gamePathHash.json"
    
    $latestJsonUrl = "https://limbus-cn.deadfish.win/latest.json"
    
    while ($true) {
        try {
            if (-not $PSCmdlet.ShouldProcess("Check and Update Limbus Translation at $gamePath")) {
                Write-Host "Skipping update check due to -WhatIf mode."
                return
            }

            $response = Invoke-RestMethod -UserAgent 'LimbusTranslationUpdater/1.0' -Uri $latestJsonUrl -UseBasicParsing
            
            if ($null -eq $response.schema_version -or $response.schema_version -ne 1) { throw "Unsupported manifest schema_version." }
            if ($null -eq $response.version -or [string]::IsNullOrWhiteSpace($response.version)) { throw "Invalid version in manifest." }
            if ($null -eq $response.package -or $null -eq $response.package.url -or $null -eq $response.package.sha256 -or $null -eq $response.package.size) {
                throw "Invalid package schema from server."
            }
            if ($response.package.sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw "Invalid SHA256 format." }
            if ($response.package.size -le 0 -or $response.package.size -gt 100MB) { throw "Invalid package size." }

            $urlObj = [System.Uri]::new($response.package.url)
            if (-not $urlObj.IsAbsoluteUri -or $urlObj.Scheme -ne 'https' -or -not [string]::IsNullOrWhiteSpace($urlObj.UserInfo)) {
                throw "Package URL must be a valid absolute HTTPS URL without credentials."
            }
            
            $packageUrl = $urlObj.AbsoluteUri
            $packageSha256 = $response.package.sha256
            $packageSize = $response.package.size
            $version = $response.version
            
            $localVersion = $null
            if (Test-Path $localVersionFile) {
                try {
                    $localVersionData = Get-Content $localVersionFile | ConvertFrom-Json
                    $localVersion = $localVersionData.version
                } catch {
                    Write-Host "Could not read local version." -ForegroundColor Yellow
                }
            }
            
            $langDir = Join-Path $gamePath "LimbusCompany_Data\Lang\LLC_zh-CN"
            $needsInstall = ($localVersion -ne $version) -or (-not (Test-Path $langDir))
            
            if (-not $needsInstall) {
                Write-Host "You already have the latest version ($version) and files exist." -ForegroundColor Green
                if (-not $Continuous) { break }
            } else {
                Write-Host "Update/Repair required! Local: $localVersion, Latest: $version" -ForegroundColor Cyan
                
                $tempPath = $null
                $mutex = New-Object System.Threading.Mutex($false, "Local\LimbusTranslationUpdater_$gamePathHash")
                if (-not $mutex.WaitOne(0, $false)) {
                    $mutex.Dispose()
                    throw "Another instance of the updater is already running for this game path."
                }
                
                try {
                    # Double-check state after acquiring mutex
                    if (Test-Path $localVersionFile) {
                        try {
                            $localVersionData = Get-Content $localVersionFile | ConvertFrom-Json
                            $localVersion = $localVersionData.version
                            $needsInstall = ($localVersion -ne $version) -or (-not (Test-Path $langDir))
                        } catch {}
                    }
                    
                    if (-not $needsInstall) {
                        Write-Host "Update handled by another instance." -ForegroundColor Green
                        if (-not $Continuous) { break } else { continue }
                    }

                    if (Get-Process -Name 'LimbusCompany' -ErrorAction SilentlyContinue) {
                        Write-Host "Limbus Company is running. Waiting for it to close..." -ForegroundColor Yellow
                        while (Get-Process -Name 'LimbusCompany' -ErrorAction SilentlyContinue) {
                            Start-Sleep -Seconds 5
                        }
                    }

                    $tempPath = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), [System.IO.Path]::GetRandomFileName())
                    $tempZip = Join-Path $tempPath "update.zip"
                    $tempExtract = Join-Path $tempPath "extract"

                    if (-not (Test-Path $appDataPath)) { New-Item -ItemType Directory -Path $appDataPath -Force | Out-Null }
                    Assert-NoLinks $appDataPath
                    if (Test-Path $localVersionFile) { Assert-NoLinks $localVersionFile }
                    if (Test-Path $lastGamePathFile) { Assert-NoLinks $lastGamePathFile }
                    
                    New-Item -ItemType Directory -Path $tempPath -Force | Out-Null
                    Assert-NoLinks $tempPath
                    
                    Write-Host "Downloading $packageUrl..."
                    Invoke-WebRequest -UserAgent 'LimbusTranslationUpdater/1.0' -Uri $packageUrl -OutFile $tempZip -UseBasicParsing
                    
                    $actualSize = (Get-Item $tempZip).Length
                    if ($actualSize -ne $packageSize) {
                        throw "Size mismatch. Expected $packageSize, got $actualSize"
                    }
                    
                    $fileHash = (Get-FileHash $tempZip -Algorithm SHA256).Hash.ToLower()
                    if ($fileHash -ne $packageSha256.ToLower()) {
                        throw "SHA256 mismatch. Expected $packageSha256, got $fileHash"
                    }
                    
                    Write-Host "Verifying archive contents..."
                    Add-Type -AssemblyName System.IO.Compression.FileSystem
                    $zipArchive = [System.IO.Compression.ZipFile]::OpenRead($tempZip)
                    $extractedSize = 0
                    $entryCount = 0
                    $caseInsensitivePaths = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)
                    
                    $tempExtractFull = (Resolve-Path -LiteralPath $tempPath).ProviderPath + "\extract\"

                    try {
                        foreach ($entry in $zipArchive.Entries) {
                            $entryCount++
                            if ($entryCount -gt 10000) { throw "Too many entries in zip archive." }
                            
                            $attr = $entry.ExternalAttributes
                            if (($attr -band 0xA0000000) -eq 0xA0000000) {
                                throw "Unix symlink detected in archive."
                            }
                            if (($attr -band 0x00000400) -eq 0x00000400 -or ($attr -band 0x4000000) -eq 0x4000000) {
                                throw "DOS reparse point detected in archive."
                            }
                            
                            if ([string]::IsNullOrWhiteSpace($entry.FullName) -or 
                                $entry.FullName.Contains(":") -or 
                                $entry.FullName.StartsWith("/") -or 
                                $entry.FullName.StartsWith("\")) {
                                throw "Unsafe path detected in zip archive: $($entry.FullName)"
                            }

                            $resolvedEntryPath = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($tempExtractFull, $entry.FullName))
                            if (-not $resolvedEntryPath.StartsWith($tempExtractFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                                throw "Zip slip detected: $($entry.FullName)"
                            }

                            $normalizedPath = $resolvedEntryPath.Substring($tempExtractFull.Length)
                            if (-not $caseInsensitivePaths.Add($normalizedPath)) {
                                throw "Duplicate path (case-insensitive) in archive: $($entry.FullName)"
                            }

                            $extractedSize += $entry.Length
                        }
                        if ($extractedSize -gt 500MB) {
                            throw "Extracted size too large ($extractedSize bytes). Possible zip bomb."
                        }
                    } finally {
                        $zipArchive.Dispose()
                    }
                    
                    Write-Host "Extracting..."
                    Expand-Archive -Path $tempZip -DestinationPath $tempExtract -Force
                    
                    $installSource = Join-Path $tempExtract "build\LLC_zh-CN"
                    if (-not (Test-Path $installSource)) {
                        throw "Invalid package structure. build\LLC_zh-CN not found."
                    }
                    
                    $installerPath = Join-Path $PSScriptRoot "Install-LimbusTranslation.ps1"
                    if (Test-Path $installerPath) {
                        Write-Host "Installing via Install-LimbusTranslation.ps1"
                        
                        & $installerPath -GamePath $gamePath -PackagePath $installSource
                        
                        $versionInfo = @{ version = $version } | ConvertTo-Json
                        $tempVersionFile = Join-Path $appDataPath ("version_" + [guid]::NewGuid().ToString("N") + ".tmp")
                        $versionInfo | Out-File $tempVersionFile -Encoding UTF8
                        Move-Item -Path $tempVersionFile -Destination $localVersionFile -Force

                        $tempPathFile = Join-Path $appDataPath ("path_" + [guid]::NewGuid().ToString("N") + ".tmp")
                        $gamePath | Out-File $tempPathFile -Encoding UTF8
                        Move-Item -Path $tempPathFile -Destination $lastGamePathFile -Force
                        
                        Write-Host "Update complete!" -ForegroundColor Green
                    } else {
                        throw "Install-LimbusTranslation.ps1 not found alongside updater."
                    }
                } finally {
                    if ($null -ne $tempPath -and (Test-Path $tempPath)) { Remove-Item $tempPath -Recurse -Force }
                    $mutex.ReleaseMutex()
                    $mutex.Dispose()
                }
            }
        } catch {
            Write-Host "Error during update: $_" -ForegroundColor Red
            if (-not $Continuous) { exit 1 }
        }
        
        if (-not $Continuous) { break }
        
        Write-Host "Waiting before next check..."
        Start-Sleep -Seconds 300
    }
}

Invoke-UpdateCheck
