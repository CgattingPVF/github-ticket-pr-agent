[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$release = Invoke-RestMethod -Uri "https://api.github.com/repos/cli/cli/releases/latest"
$zipAsset = $release.assets | Where-Object { $_.name -match '^gh_.+_windows_amd64\.zip$' } | Select-Object -First 1
$checksumsAsset = $release.assets | Where-Object { $_.name -match '^gh_.+_checksums\.txt$' } | Select-Object -First 1

if (-not $zipAsset -or -not $checksumsAsset) {
    throw "The latest GitHub CLI release does not contain the expected Windows archive or checksums."
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ticket-pr-agent-gh-" + [guid]::NewGuid())
$zipPath = Join-Path $tempRoot $zipAsset.name
$checksumsPath = Join-Path $tempRoot $checksumsAsset.name

try {
    New-Item -ItemType Directory -Path $tempRoot | Out-Null
    Invoke-WebRequest -Uri $zipAsset.browser_download_url -OutFile $zipPath
    Invoke-WebRequest -Uri $checksumsAsset.browser_download_url -OutFile $checksumsPath

    $escapedName = [regex]::Escape($zipAsset.name)
    $checksumLine = Get-Content $checksumsPath | Where-Object { $_ -match "^([0-9a-fA-F]{64})\s+$escapedName$" } | Select-Object -First 1
    if (-not $checksumLine) {
        throw "No SHA-256 checksum was published for $($zipAsset.name)."
    }

    $expectedHash = ([regex]::Match($checksumLine, '^([0-9a-fA-F]{64})')).Groups[1].Value.ToUpperInvariant()
    $actualHash = (Get-FileHash -Algorithm SHA256 $zipPath).Hash.ToUpperInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "GitHub CLI checksum verification failed."
    }

    Expand-Archive -Path $zipPath -DestinationPath $tempRoot
    $ghExecutable = Get-ChildItem -Path $tempRoot -Filter gh.exe -Recurse | Select-Object -First 1
    if (-not $ghExecutable) {
        throw "gh.exe was not found in $($zipAsset.name)."
    }

    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Copy-Item $ghExecutable.FullName (Join-Path $Destination "gh.exe") -Force
    Write-Host "Bundled GitHub CLI $($release.tag_name)."
}
finally {
    if (Test-Path $tempRoot) {
        Remove-Item $tempRoot -Recurse -Force
    }
}
