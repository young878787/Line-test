# LINE Weather Galgame Bot

目前以 [n8n 常駐服務](n8n/README.md) 為主要入口，重用 Python 業務核心與 SQLite，提供 LINE 角色聊天、全臺 22 縣市天氣問答、每日六句晨報、降雨變化提醒與背景記憶。地區維持必填，只需有效縣市，不必提供行政區；詳見 [縣市預報與設定](n8n/docs/縣市預報與設定.md)。

完整檔案架構、資料流及驗證結果見 [實作架構與驗證](docs/實作架構與驗證.md)；需求見 [原始計畫](docs/LINE_Weather_Galgame_Bot_Plan.md)。

## 啟動

```powershell
uv sync --locked --group dev
Copy-Item .env.example .env
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_cloudflared.ps1
```

已有 `.env` 時直接補齊缺少的設定，不要覆蓋原檔。必要設定：

```dotenv
CWA_API_KEY=
LINE_CHANNEL_ACCESS_TOKEN=
LINE_CHANNEL_SECRET=
AI_API_Keys=
AI_API_URL=https://chatapi.ntubimdbirc.tw
AI_MODEL=BIRC-LLM
LINE_MONTHLY_LIMIT=200
DATABASE_PATH=bot.sqlite3
LOCAL_TUNNEL_ENABLED=true
```

`AI_API_Keys` 為單一 Bearer token。AI URL 可填根網址、`/v1` 或完整 `/v1/chat/completions`。模型須支援 JSON Schema 結構化輸出；thinking 固定關閉。聊天整體處理期限為 25 秒，LINE 回覆期限為 55 秒；模型失敗使用短備援，記憶工作另行執行。

```powershell
uv run python -m weather_bot.main
```

服務獨占 `127.0.0.1:18567`，LINE webhook 為 `/webhook`，健康檢查為 `/health`。原啟動指令會一起啟動 Cloudflare Quick Tunnel，使用臨時 HTTPS 網址，不需購買網域或登入 Cloudflare。Windows 安裝腳本從官方 release 下載固定版本並檢查 SHA-256，執行檔保存在忽略版控的 `.tools`；其他平台可將官方 `cloudflared` 放入 PATH。

請在 LINE Developers 開啟 **Use webhook**。程序會先驗證公開 `/health`、有效與無效簽章，再讓 LINE 測試候選網址；通過後自動更新 webhook 並確認回讀。每次重啟都會同步新的 `https://….trycloudflare.com/webhook`，不必手動複製。LINE 設定可能快取最多一分鐘；以 `logs/service/latest.json` 的 `status: ready` 與 `checks` 判斷是否就緒。詳細隧道日誌固定為 `logs/service/cloudflared.log`，重啟驗證結果為 `logs/service/restart_test.json`。

保持啟動視窗運行，按 `Ctrl+C` 可停止服務與隧道；再次執行相同啟動指令即可重開。隧道子程序退出時會在 60 秒後重建並同步新網址，驗證或 API 失敗也會記錄原因並重試。若 `18567` 已被佔用，啟動會直接失敗，不更動 LINE 設定。電腦休眠、關機或服務停止期間無法接收訊息。[Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) 是測試用臨時服務，沒有可用性保證。

已有固定 HTTPS 反向代理時，可設 `LOCAL_TUNNEL_ENABLED=false`；使用持久磁碟並維持單一程序、單一 Web worker。反向代理不得改寫驗簽所用的原始 body。僅透過 `python -m weather_bot.main` 啟動才自動管理本機隧道，匯入 ASGI app 與 CI 不會啟動隧道或改動 LINE 設定。

正式資料庫預設為專案根目錄的 `bot.sqlite3`，服務啟動時建立七張表。相對 `DATABASE_PATH` 一律以專案根目錄解析。此檔案與 WAL／SHM 不納入版控；不要將 CI 資料庫當作正式資料庫。

加入 LINE 好友後提供必填縣市（例如 `臺中市`）、起床時間（例如 `07:00`）、晨報與夜間提醒選擇，再提供偏好或回覆 `先跳過`，最後回覆 `確認開始`。可一次說「我住新北市，早上 7.30 起床，叫我海鹽布丁，口吻溫柔一點」，也可分次提供；只提供有效縣市即可繼續，不追問行政區。`嘉義`、`新竹` 須說明是縣或市，缺少有效縣市不能完成設定。確認前可直接補充或更正，其他有效答案會保留。

尚未完成新手引導時，文字、數字、舊按鈕或貼圖一律優先進入引導流程。一般輸入透過 LINE Reply 回覆目前設定與下一題。標準格式直接解析，自然語句只呼叫設定抽取 AI，檢查逐字來源、通知種類與開關及有效縣市；不進行一般 AI 聊天、不推播。資料收齊仍須明確確認，才把正式設定與明確說出的初始偏好一起儲存並開放聊天。偏好可跳過，不猜稱呼、口吻或其他習慣。

LINE 原文在接收時與工作一起入庫；引導草稿保存在 `users.setup_data`，重啟不遺失。地區、起床時間與通知仍以 `users` 正式欄位為準；初始稱呼、口吻、怕冷等寫入附來源的 `memories`。完成後 AI 上下文同時讀取正式設定與偏好。既有資料庫會自動加上草稿欄位，保留使用者及原訊息。舊版尚未完成且未解析的最近原文，會在下一則設定輸入時一併整理；不重用舊 Reply token。

每輪三個接話 action 由 AI 根據當前情境產生一小段使用者說得出口的文字。正文顯示完整段落，Quick Reply 按鈕使用短標籤，選取後顯示並送入相同的完整段落。只有錯誤備援與固定設定仍使用程式模板。

固定設定命令：`設定`、`修改地區`、`修改起床時間`、`開啟晨報`、`關閉晨報`、`開啟天氣提醒`、`關閉天氣提醒`、`開啟夜間提醒`、`關閉夜間提醒`。

舊的 `--now` 直接推播入口已由新服務與 outbox 取代；`sample/opencode.py` 不參與執行。

## 測試與報告

```powershell
uv run python -m pytest -q --junitxml=logs/ci/junit.xml
```

一般測試不呼叫外部服務，使用 `logs/ci/databases/<案例名稱>.sqlite3`、固定天氣、可控制時鐘及模型／LINE mock。每案例固定保留最新資料庫，重跑只覆寫帶 CI 標記的測試產物；沒有 CI 標記的檔案會拒絕覆寫。報告固定覆寫 `logs/ci/latest.json`，包含資料庫路徑、每個案例狀態、Reply 實測秒數、單則訊息數量與記憶／工作證據。Reply mock 拒絕重用及逾期 token；另有慢記憶仍在執行時已完成回覆的測試。真實 API／模型驗證也使用此 CI 資料庫目錄，不寫入正式庫。

```powershell
uv run python -m pytest tests/live -q --live
```

真實驗證使用設定中的 CWA 與 BIRC；T01–T05 最多八次模型呼叫，T06 五輪聊天最多五次，T07 新手引導與確認後聊天最多三次，全套最多十六次。LINE 仍為 mock，不向使用者發訊。原本驗收輸出 `logs/live/latest.json`；五輪完整對話與 action 輸出 `logs/live/chat_five_turns.md`，T07 逐步引導與偏好使用輸出 `logs/live/onboarding.md`，機器判讀證據為同名 `.json`。缺少金鑰為 `NOT_RUN`，不當作通過。少量真實案例通過不代表完整語意品質或真實手機收訊驗收。

只執行真實 AI 五輪聊天：

```powershell
uv run python -m pytest tests/live/test_chat_five_turns_live.py -q --live
```

一般 CI 也執行相同五輪入口與選項解析測試，使用模型替身，產物為 `logs/ci/chat_five_turns.md`／`.json`。手動真實驗證 job 會執行實際 BIRC 的五輪版本並上傳報告。

GitHub Actions 在 PR／主要分支 push 執行 Linux、Windows 測試；實際 CWA／模型驗證僅允許預設分支的手動觸發，需設定 `CWA_API_KEY`、`AI_API_KEYS`、`AI_API_URL`、`AI_MODEL` secrets。每次都上傳最新報告，失敗也保留結果。
