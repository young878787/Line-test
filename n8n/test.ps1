$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    & uv run python n8n/tests/run_tests.py
    if ($LASTEXITCODE -ne 0) { throw '驗證失敗，詳見 n8n/logs。' }
} finally { Pop-Location }
