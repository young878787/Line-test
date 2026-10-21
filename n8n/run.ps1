param([switch]$Mock)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    if ($Mock) { & uv run python n8n/service.py --mock }
    else { & uv run python n8n/service.py }
    if ($LASTEXITCODE -ne 0) { throw '服務啟動失敗，請查看 n8n/.runtime/service.json。' }
} finally { Pop-Location }
