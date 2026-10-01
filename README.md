# ncdr-alert

NCDR 桃園市即時示警 Telegram Bot
每 10 分鐘從 NCDR 民生示警公開資料平台取得「即時示警」，
只推播 生效時間為今日（台灣時間）、影響範圍含桃園市 的示警。
目錄
    .github/workflows/ncdr-alert.yml   排程（*/10）
config/config.json                 areas 等設定
src/main.py ncdr.py telegram.py state.py
data/sent_alerts.json              已推播紀錄（由 workflow 自動提交）

設定
Repo -> Settings -> Secrets and variables -> Actions，新增：
`TELEGRAM\_BOT\_TOKEN`、`TELEGRAM\_CHAT\_ID`
Settings -> Actions -> General -> Workflow permissions：選 Read and write permissions
手動執行一次：Actions -> NCDR Alert Bot -> Run workflow
去重規則
同一示警 ID 且內容未變 -> 不再推播；內容變更 -> 推播一次「更新」。
判斷依據
影響範圍：CAP `geocode`（Taiwan_Geocode_113 優先，缺則 103），縣市前兩碼 68 = 桃園市
預設不以 `areaDesc` 文字判斷；如需啟用，`allow\_areadesc\_text\_fallback` 設為 true
