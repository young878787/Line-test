$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$toolDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../.tools'))
$destination = Join-Path $toolDirectory 'cloudflared.exe'
$download = Join-Path $toolDirectory 'cloudflared.download'
$version = '2026.10.2'
$expectedHash = '12c7c887f8aaab779aef1dfa9b60673530a5adb88ecc55dd6df8f56e0b70f199'
New-Item -ItemType Directory -Force -Path $toolDirectory | Out-Null
if ((Test-Path -LiteralPath $destination) -and
    ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -eq $expectedHash)) {
    Write-Output "cloudflared $version already installed."
    exit 0
}
try {
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/cloudflare/cloudflared/releases/download/$version/cloudflared-windows-amd64.exe" -OutFile $download
    if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $expectedHash) {
        throw 'cloudflared SHA-256 verification failed.'
    }
    Move-Item -LiteralPath $download -Destination $destination -Force
    Write-Output "Installed cloudflared $version (SHA-256 verified)."
} finally {
    if (Test-Path -LiteralPath $download) { Remove-Item -LiteralPath $download }
}
