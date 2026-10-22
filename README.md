# LINE Weather Galgame Bot

結合天氣提醒與個人化 AI 聊天的 LINE 助理：少一點每天查預報的麻煩，多一個提醒你帶傘、也能陪你聊天的角色。

本專案從 Python 版本開始，之後透過 Skills 與 MCP 輔助整合成 n8n 版本。目前以 **n8n 編排流程、Python 執行業務邏輯、SQLite 保存資料**，提供新手引導、天氣查詢、預報變更提醒、聊天記憶與 LINE Quick Reply。

簡潔的作品說明可直接閱讀 [專案介紹](docs/專案介紹.md)。

## 專案背景與想解決的問題

這個作品的起點很生活化：每天主動查天氣很麻煩，有時早上看起來不會下雨，午餐出門就忘記帶傘，結果在路上淋雨；即使早上看過預報，也不一定會注意到後續變化。

因此，我想讓天氣資訊主動出現在平常使用的 LINE 裡，整理當天預報，並在預報改變時提醒帶傘。剛好也在嘗試 AI 聊天，便加入個人化口吻、記憶更新與新手設定，讓它成為有互動感的日常助理。

## 目前可以做什麼

| 功能 | 使用者能獲得的體驗 |
|---|---|
| 新手引導 | 逐步設定縣市、起床／晨報時間、晨報與夜間提醒開關，以及稱呼或聊天口吻；確認後開始使用 |
| 全臺天氣查詢 | 支援全臺 22 縣市，包含澎湖、金門與連江；可以詢問其他縣市，不會因此改掉原本的提醒地區 |
| 晨報與預報變更提醒 | 整理天氣、氣溫、體感與降雨機率；預報有變化時主動通知，協助判斷是否帶傘或調整外出準備 |
| 個人化聊天 | 結合近期對話、使用者設定與已保存的偏好回覆，可更改稱呼及溫柔、元氣、淡定等支援的口吻 |
| 背景記憶更新 | 整理使用者明確提供的偏好與資訊，供後續對話使用，並支援更正與忘記 |
| Quick Reply 選項 | 一般聊天每輪提供三個接話選項，像 Galgame 一樣點選回覆，也能隨時自由輸入文字 |

天氣資料來自中央氣象署，提醒以設定縣市的預報與定期更新為依據。目前提供縣市級資訊，尚未提供沿途定位或即時降雨偵測。

## 使用流程

1. **完成初始設定**：加入 LINE 好友後，依引導提供縣市、時間與通知選擇。偏好可以先跳過；資料收齊後仍須明確回覆「確認開始」。
2. **查天氣或聊天**：例如「臺中市今天會下雨嗎？」或直接聊日常，也可以點選回覆下方的 Quick Reply。
3. **接收主動提醒**：依設定收到晨報與預報變更通知。尚未起床或關閉夜間提醒時，系統會依規則延後通知。
4. **隨時調整設定**：例如「晨報時間改成早上八點半」「改住澎湖縣」「叫我阿洋」，或告訴它希望使用支援的聊天口吻。

初次設定可以一次說「我住新北市，早上 7:30 起床，叫我海鹽布丁，口吻溫柔一點」，也能分次提供。未填完的有效答案會保留，完成引導前優先處理設定。

輸入 `設定` 可查看目前內容；也支援 `修改地區`、`修改起床時間`、`開啟晨報`／`關閉晨報`、`開啟天氣提醒`／`關閉天氣提醒`、`開啟夜間提醒`／`關閉夜間提醒`。

## 做法與開發過程

先以 Python 建立聊天、天氣、記憶、新手引導與測試基準，再透過 Skills 提供操作指引、MCP 輔助建立與驗證 n8n workflow，將觸發、排程、HTTP 呼叫與錯誤處理整合到可視化流程。

目前的資料流為：

```text
LINE → Cloudflare Quick Tunnel → 公開 gateway → n8n workflow
                                                ↓
                                          Python adapter
                                                ↓
                                     SQLite／氣象署／AI／LINE
```

- **n8n**：以單一 workflow 處理收件、聊天工作、背景排程與錯誤路徑。
- **Python**：沿用既有業務核心，負責設定、預報比較、聊天與記憶驗證，以及 LINE 傳送。
- **SQLite**：保存使用者設定、對話、記憶、預報與待處理工作，讓流程可在重啟後繼續。
- **AI**：將既有天氣資料轉成易讀文字、生成聊天與接話選項，並提出記憶更新；程式負責檢查格式與來源。

n8n 版本仍依賴 Python adapter，匯入 workflow JSON 後還需要搭配服務環境。主要入口為 [`n8n/service.py`](n8n/service.py)，流程生成來源為 [`n8n/scripts/build_workflows.py`](n8n/scripts/build_workflows.py)。

## 執行與開發

### 準備環境

使用 Python 3.11 以上與 uv。n8n 版本另需 Node.js 22.22 以上、全域 `n8n@2.35.7` 與 cloudflared；環境說明見 [n8n README](n8n/README.md)。

從專案根目錄執行；已有 `.env` 時保留原檔，只補齊設定：

```powershell
uv sync --locked --group dev
if (!(Test-Path .env)) { Copy-Item .env.example .env }
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_cloudflared.ps1
```

依 [環境變數範本](.env.example) 設定：

| 設定 | 用途 |
|---|---|
| `CWA_API_KEY` | 中央氣象署資料授權 |
| `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_CHANNEL_SECRET` | LINE 傳送與 webhook 驗簽 |
| `AI_API_Keys`、`AI_API_URL`、`AI_MODEL` | AI Bearer token、API 網址與模型；模型須支援 JSON Schema 結構化輸出 |
| `LINE_MONTHLY_LIMIT`、`DATABASE_PATH` | 推播配額控制與業務資料庫位置 |

`.env`、正式 SQLite、`n8n/.runtime/` 及日誌皆為本機資料，不納入版控。

### 啟動目前的 n8n 版本

```powershell
uv run python n8n/service.py
```

正式模式會沿用業務資料庫、建立公開 HTTPS，並驗證及同步真實 LINE webhook。先停止舊 Python 服務，再啟動此入口；終端機需保持運行，在 LINE Developers 開啟 **Use webhook**。

編輯介面為 <http://127.0.0.1:5680>。查看 `n8n/.runtime/service.json`，待 `status: ready` 且 `checks.endpoint_matches: true` 後，再進行 LINE 實測。按 `Ctrl+C` 停止。

僅使用隔離資料與模擬外部 API 時：

```powershell
uv run python n8n/service.py --mock
```

Mock 模式仍會初始化／更新專用 n8n 與 workflow，但不建立公開隧道、不更新真實 LINE endpoint。完整操作與切換方式見 [使用與驗證](n8n/docs/使用與驗證.md)。

## 成果與驗證

目前已串起「初始設定 → 天氣整理與提醒 → 個人化聊天 → 記憶更新」的使用流程，並提供按鈕選項與自由輸入兩種互動方式。

既有驗證紀錄涵蓋 Python 業務回歸、adapter 隔離測試、真實 n8n 引擎搭配 mock 業務，以及程序恢復與公開 HTTPS。各層測試範圍與結果見 [使用與驗證](n8n/docs/使用與驗證.md) 及 [縣市預報與設定](n8n/docs/縣市預報與設定.md)。

不需啟動 n8n 的測試入口：

```powershell
uv run python -m pytest -q --junitxml=logs/ci/junit.xml
uv run python n8n/tests/test_service.py
```

第一條使用模型／LINE 等替身，報告固定為 `logs/ci/latest.json`；第二條驗證 adapter 與生命週期，報告為 `n8n/logs/service_tests.json`。根目錄 pytest 不會自動執行 `n8n/tests/`。

真實 n8n 引擎、公開 HTTPS、真實 CWA／AI 與手機收訊分別驗證；mock 通過不能直接代表真實 LINE 業務或長時間運行已驗收。`tests/live` 需明確加上 `--live`，會呼叫實際 CWA／AI 並可能消耗額度，LINE 仍為 mock。

## 未來方向

下一步希望加入完整的聊天情緒系統：讓 AI 判斷每輪互動對角色情緒的影響，累積加分或減分，再反映到後續的語氣與回應，增加對話的連續性與角色感。

目前已有聊天口吻與偏好設定，情緒分數及其對話機制尚未實作。

## 延伸文件

- [專案介紹](docs/專案介紹.md)：背景、問題、做法、成果與未來方向的簡潔書面版本。
- [n8n README](n8n/README.md)：目前主要服務入口與環境需求。
- [使用與驗證](n8n/docs/使用與驗證.md)：啟停、切換、測試範圍與證據位置。
- [n8n 實作轉換分析](n8n/docs/實作轉換分析.md)：目前架構、資料流與執行邊界。
- [縣市預報與設定](n8n/docs/縣市預報與設定.md)：22 縣市查詢與提醒地區的規則。
- [Python 初版架構與驗證](docs/實作架構與驗證.md)、[原始計畫](docs/LINE_Weather_Galgame_Bot_Plan.md)：開發歷程參考；現行入口與地區支援以 n8n 文件及程式為準。
