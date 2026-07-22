# Log Triage & Skill Learning

一個獨立於 [wireless_ce_avatar/IntelAvatar](https://github.com/kj-fang/wireless_ce_avatar) 之外的精簡版系統，保留其中三個核心能力：

1. **Log Viewer** — 手動選擇 log 檔與 `.tat` 過濾檔（TextAnalysisTool.NET 格式），勾選/排除關鍵字後即時預覽過濾結果（等同工程師現在用的 log 系統）。
2. **Chatbot** — 針對目前過濾出來的 log 內容與 LLM 對話分析。
3. **Teach Skill（學習迴圈）** — 系統觀察你目前使用的過濾情境（tat 關鍵字 + 過濾結果 + 對話紀錄），主動提出幾個釐清問題；你回答後，LLM 會把知識整理成 `data/skills/skills.yaml` 裡的一筆 skill（`keywords` / `exclusive` / `expert_rules`），可再手動編輯後儲存。之後這些 skill 可在 Log Viewer 直接載入重用。

移除了原本 IntelAvatar 中與本次需求無關的部分（case number 下載、BSOD、BT/NW 專用流程、ETL 自動化、SendTo 整合等），只保留 log 檢視/過濾 + chatbot + skill 學習這條主線，並改用**手動檔案選擇**（因為 `\\infs089.iil.intel.com\...\log_parser_data\filter` 這類共用磁碟機在你的環境連不到）。

## 安裝

```powershell
pip install -r requirements.txt
```

## 設定 LLM（GNAI / Anthropic 相容端點）

連線方式與 IntelAvatar 相同：優先讀取公司共用資料夾的 `keys.py`（`gnaigpt_token` / `gnaigpt_url` / `gnaigpt_model`），若連不到，改用本機設定檔開發測試 —— 直接把你自己的 `key.py` 內容存成 `configs\keys_local.py`（同 schema，已 gitignore）：

```powershell
# 建立 configs\keys_local.py，內容同你的 key.py：
#   gnaigpt_token = "..."
#   gnaigpt_url   = "https://gnai.intel.com/api/providers/anthropic"
#   gnaigpt_model = "claude-4-6-sonnet"
```

`configs/path_configs.py` 中的 `KEY_PATH_prim` / `KEY_PATH_bkup` 已指向與 IntelAvatar 相同的共用路徑，VPN 可連到時會自動優先使用共用資料夾的 key.py。

## 執行

```powershell
python app.py
```

開啟 http://127.0.0.1:5000

## 目錄結構

```
app.py                  Flask 進入點
configs/                連線設定、全域狀態
services/               LLM 連線、skill 儲存、log 過濾、學習迴圈邏輯
utils/                  .tat 解析、檔案選擇對話框、共用工具
blueprints/             main / log_viewer / chatbot / learning / skills
templates/, static/     前端頁面
data/skills/skills.yaml 已學習的 skill 知識庫（keywords/exclusive/expert_rules）
```
