# LINE Weather Bot：單一 n8n workflow 常駐服務

以 **一個 n8n workflow＋業務 adapter** 接替原本 `weather_bot.main` 的服務入口。Webhook、聊天補跑、每秒背景排程與錯誤處理放在同一個 workflow；聊天、天氣、記憶與 SQLite 契約由 adapter 重用。

新手流程為「縣市→起床／晨報時間→晨報→夜間→偏好或跳過→確認開始」，完成後可口語更正。白天預報變更自動提醒；夜間延後與配額不足保存在 SQLite 工作中，晨報只消除已明確顯示的變更。`Weather Outbox and Memory` 的 execution 輸出包含 `jobs`、`notices` 狀態統計，HTTP 成功不代表所有訊息已送達；完整業務案例見根目錄 `tests/test_setup_notifications.py` 及 `n8n/tests/test_workflows.py`。

從 `D:\Line-test` 啟動，指令會持續執行：

```powershell
uv run python n8n/service.py
```

先在原終端機停止舊 Python 服務；18567 還在使用時，新入口會拒絕啟動。正式啟動讀取根目錄 `.env`、沿用正式業務 DB，取得新的公開 HTTPS，完成健康與驗簽測試後，自動驗證及同步 LINE webhook。沒有移入 5678。

- 編輯：<http://127.0.0.1:5680>。本機帳號在 `n8n/.runtime/account.json`，與 5678 分開。
- 狀態：`Get-Content n8n/.runtime/service.json -Raw`。
- 停止：原終端機 `Ctrl+C`，或另一個 PowerShell 執行 `n8n/stop.ps1`。
- 隔離運行：`uv run python n8n/service.py --mock`，不建立隧道、不更新 LINE。
- 完整驗證：`n8n/test.ps1`。業務 API 全部 mock，另以真實 HTTPS 探測空事件，不更新 LINE endpoint。

前置環境：既有 uv 環境、Node.js 22.22 以上、全域 `n8n@2.35.7`，以及 `.tools/cloudflared.exe` 或 PATH 中的 cloudflared。

- [操作、切換與驗證](docs/使用與驗證.md)
- [全臺縣市預報與必填設定](docs/縣市預報與設定.md)
- [詳細實作與資料流](docs/實作轉換分析.md)
- [啟動空白／120 秒逾時的診斷與修正](docs/啟動逾時修正.md)
- [唯一 workflow 原碼](workflows/workflow.js)／[匯出](workflows/workflow.json)／[本機編輯網址](workflows/manifest.json)
- [業務結果](logs/latest.json)／[服務測試](logs/service_tests.json)／[程序恢復](logs/runtime_tests.json)／[公開 HTTPS](logs/public_https.json)

本交付仍保留 Python adapter，不再啟動原 Python HTTP 主程式或輪詢 worker。Workflow JSON 需要搭配 adapter。縣市擴充已通過真實 CWA 唯讀驗證及 n8n 引擎的業務 mock 測試；真實 LINE／AI 業務切換及手機收訊仍待驗收。測試結束會停止專用實例並保留資料。
