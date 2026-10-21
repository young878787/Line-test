$ErrorActionPreference = 'Stop'
$runtimeRoot = Join-Path $PSScriptRoot '.runtime'
$statePath = Join-Path $runtimeRoot 'service.json'
if (-not (Test-Path -LiteralPath $statePath)) { throw '沒有常駐服務狀態。' }
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if ($state.status -eq 'stopped') { Write-Output '服務已停止。'; return }
$key = (Get-Content -LiteralPath (Join-Path $runtimeRoot 'internal-key.json') -Raw | ConvertFrom-Json).key
$null = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:5682/service/stop' -Headers @{'X-Line-N8n-Key'=$key}
for ($attempt=0; $attempt -lt 45; $attempt++) {
    Start-Sleep -Seconds 1
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if ($state.status -eq 'stopped') { Write-Output '已停止 n8n、adapter 與公開隧道，資料保留。'; return }
}
throw '停止尚未完成，請檢查 n8n/.runtime/service.json。'
