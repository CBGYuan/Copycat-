# 測試腳本 — WiFi Deauth / AP 週期性評分場景

**素材**：`WifiDriverIHVSession.etl.003.log`（101,168 行，capture 時間
09:53:47 → 11:26:42，約 93 分鐘）＋ `connectivity (1).tat`

這份文件不是憑空編的操作腳本 —— 我先用 Python 把 `.tat` 裡每一個已啟用
filter 的實際命中數在這支 log 裡跑過一遍（TAT 的 `matches_text` 是**字面字串
比對**，不是 regex，統計時要注意），抓到兩個值得在 `copycat` 面板裡實際教學
的真實現象。以下每一步都附上真實行號 / 時間戳可以核對。

---

## 讀 log 得到的關鍵發現

### 發現 1 — 這份 capture 沒有「全新連線」或「WoWLAN 睡眠」場景，好幾個
already-enabled 的 include filter 是 0 命中的死關鍵字

| 已啟用的 include 關鍵字 | 這份 log 命中數 |
|---|---|
| `Assert` | 0 |
| `TASK_DISCONNECT[8]` | 0 |
| ` ------- RESUME FLOW` | 0 |
| `** SUSPEND` | 0 |
| `WDI_ASSOC_STATUS_SUCCESS` | 0 |
| `CONNECTED - to: ` | 0 |
| `TASK_Connect` | 0 |
| `Set SSID` | 0 |
| `[ATTEMPT_TO_CONNECT]` | 0 |
| `NIC State` | 0 |
| `wakeup_by` | 0 |
| `TASK ROAM` | 0 |
| `candidate grade` | 0 |
| `WoWLAN is active!!!` | 0 |
| ` ---------- SUSPEND FLOW FINISHED ----------` | 0 |

這份 log 是「已連線狀態下的持續觀察」，不是連線建立、也沒有進入
suspend/resume/WoWLAN 週期 —— 所以上面這些關鍵字在**這一支 log** 完全打不到
任何東西。

### 發現 2 — `candidate grade` 打不到，但 `ApSelectionApply` / `Best Candidate` 打得到

| 關鍵字 | 命中數 |
|---|---|
| `candidate grade` | 0 |
| `` | 102 |
| `Best Candidate` | 308 |

代表這個 firmware 版本目前實際印出的字串是 `prvApSelectionApplyGradeBonuses`
/ `prvApSelectionChooseBestCandidate` / `prvhApSelectionPrintBestCandidates`
這一組，而不是含 "candidate grade" 字面文字的舊版字串 —— `candidate grade`
這個關鍵字對目前的 build 是**過時字串**。

真實範例（第 4265–4271 行，10:37:57）：
```
[AP_SELECTION] [prvApSelectionApplyGradeBonuses]:Applying grade bonuses (if applicable): Address(7C:10:C9:69:EF:A8)
[AP_SELECTION] [prvApSelectionChooseBestCandidate]:Best candidate with grade 1348620 is:
[AP_SELECTION] [prvhApSelectionPrintBestCandidates]:[Best Candidate 0]: band:2, channel:37, BW:160MHz, RSSI:-56, tput:2074800, chLoad:35, ...
```
102 次 `ApSelectionApply` 全部都是同一個 Address(7C:10:C9:69:EF:A8) —— 這台
裝置身邊只有**一個 AP**（`ASUS_AXE11000_6G`），所以 AP 評分是週期性的背景
重新評分（每 2–6 分鐘一次，10:37→10:51 抓到 6 次），**不是**在挑選要漫遊去
哪個候選 AP —— 沒有第二個 AP 可以漫遊，`TASK ROAM` 也因此 0 命中，故事一致。

### 發現 3（最重要）— `stateMachineSetStateNoCurrentFlow` 目前被設成「排除」，
但它其實是唯一一次 Deauth 事件的狀態機軌跡，被誤判成雜訊排掉了

`.tat` 裡這條是 `excluding="y"`（排除）：
```xml
<filter enabled="y" excluding="y" ... text="stateMachineSetStateNoCurrentFlow" />
```

這份 log 裡 `DeAuth` 只出現 **6 次**，`stateMachineSetStateNoCurrentFlow` 也
剛好 **6 次** —— 但不是巧合。第 99038–99495 行，11:25:49.899–50.010（唯一一次
斷線事件）：

```
99038  [BSS_VIF][stateMachineSetStateNoCurrentFlow]: CONNECTED.IDLE --> TERMINATION_DEAUTH.TX_FLUSH
99076  [BSS_VIF][stateMachineSetStateNoCurrentFlow]: TERMINATION_DEAUTH.TX_FLUSH --> TERMINATION_DEAUTH.DEAUTH
99079  [CNCT_FLOW][hmfmEvMgmtFrameCreate]: DEAUTH_REQ - sent to: ASUS_AXE11000_6G 7C:10:C9:69:EF:A8, channel=37, band=6_7GHz
99081  [POLICY][hPolicyHandleEvTxDeauthReqFill]: Deauth reason: 0x1, Deauth termination reason: 0x0
99097  [BSS_VIF][stateMachineSetStateNoCurrentFlow]: TERMINATION_DEAUTH.DEAUTH --> REMOVE_PEER
99494  [BSS_VIF][stateMachineSetStateNoCurrentFlow]: REMOVE_PEER --> SET_LMAC_DEFAULTS
99495  [BSS_VIF][stateMachineSetStateNoCurrentFlow]: SET_LMAC_DEFAULTS --> POST_TERMINATION
```

`hPolicyHandleEvTxDeauthReqFill`（**Tx** = 發送）代表這是**裝置端主動送出**
的 deauth，不是 AP 踢掉裝置。目前這個 filter 把整條「CONNECTED.IDLE →
TERMINATION_DEAUTH.TX_FLUSH → DEAUTH → REMOVE_PEER → SET_LMAC_DEFAULTS →
POST_TERMINATION」的斷線狀態機骨架**整條排除掉**了 —— 對於「排查 deauth 根因」
這個場景，這個關鍵字其實是主要證據，不是噪音。

（第 6 次出現在第 164 行、09:54:36，是正常連線初期的
`CONNECT.PMKID_PARAMS_NEEDED --> CONNECT.SESSION_PROTECTION` 轉場，跟斷線
無關 —— 所以這個關鍵字是「平常是雜訊，但斷線發生時是關鍵證據」的**條件性**
知識，這正是要教給系統的重點。）

### 發現 4 — `PROP_SET_` 目前排除，這個判斷是對的（不用動）

18 次命中全部是 `PROP_SET_CONNECTION_QUALITY` / `PROP_SET_ADD_CIPHER_KEYS` /
`PROP_SET_MULTICAST_LIST` 這類連線建立時的例行 OID 設定回報，跟斷線根因無關
——這是一個「原本判斷就正確」的對照組，不是每一步都要挑錯。

---

## Step-by-step 操作腳本

> 對應你面板上的真實按鈕：Log 檔選取、TAT Filter 表格的 checkbox / “+”、
> Steps 面板的 🎓、Log Round & Analyze、Export Skill。

### Step 0 — 準備
1. **Log** 欄位 📁 選 `WifiDriverIHVSession.etl.003.log`
   → 應自動偵測 domain badge 顯示 **WiFi**（log 內容含 `CNCT_FLOW`/`WDI_`
   等 WiFi 專屬標記）。
2. **TAT Filter** 卡片 📁 選 `connectivity (1).tat`
   → filter 表格會列出全部 86 條規則，套用後 Filtered Log 立刻跑一次。

### Step 1 — 觀察初次結果，注意 Hits 欄
套用後看 filter 表格的 **Hits** 欄，你會看到上面「發現 1」列的那些關鍵字
Hits 全部是 0（跟我離線統計的結果一致）。這是第一個 Log Round 前，工程師
自然會做的檢查。

### Step 2 — 關掉這次用不到的 include filter（乾淨化這次的排查範圍）
逐一取消勾選以下（Hits=0，這次 session 用不到，不代表永久刪除）：
`Assert`、`TASK_DISCONNECT[8]`、` ------- RESUME FLOW`、`** SUSPEND`、
`WDI_ASSOC_STATUS_SUCCESS`、`CONNECTED - to: `、`TASK_Connect`、`Set SSID`、
`[ATTEMPT_TO_CONNECT]`、`NIC State`、`wakeup_by`、`TASK ROAM`、
`candidate grade`、`WoWLAN is active!!!`、
` ---------- SUSPEND FLOW FINISHED ----------`

每次取消勾選都會自動重跑 filter（`toggleFilter` → `debounceApplyFilter`）。
因為這些關鍵字 unique_hits 本來就是 0，紅旗偵測不會跳出「losing
load-bearing」警告 —— 這批操作很安全，適合當「暖身」。

### Step 3 — 揭穿被誤判的 exclude：`stateMachineSetStateNoCurrentFlow`
1. 在 filter 表格找到 `stateMachineSetStateNoCurrentFlow`（紅色 EXCL 標籤），
   **取消勾選**它 → 這是一個 `toggle_off` 操作，會記錄進 Operation
   journal / Steps。
2. 在下方「Add a filter keyword...」輸入框輸入
   `stateMachineSetStateNoCurrentFlow`，右邊型別選 **Include**，按 “+”
   → 這是 `add_include`。
3. 重新套用後，Filtered Log 應該會多出第 99038/99076/99097/99494/99495 這
   5 行斷線狀態機轉場（第 164 行那個連線初期的轉場也會一起顯示，這是預期
   內、可以留著）。
4. 在 **Steps** 面板找到剛剛那個 `add_include "stateMachineSetStateNoCurrentFlow"`
   的項目，點 🎓：
   > **教學內容（照這樣打進 textarea）**：
   > This string is normally just a routine transition in the BSS_VIF state machine during connection establishment (e.g., CONNECT.PMKID_PARAMS_NEEDED → CONNECT.SESSION_PROTECTION). However, the same log point is also the only complete state machine trace recorded in the deauth disconnection process: CONNECTED.IDLE → TERMINATION_DEAUTH.TX_FLUSH → TERMINATION_DEAUTH.DEAUTH → REMOVE_PEER → SET_LMAC_DEFAULTS → POST_TERMINATION. This keyword must be retained when investigating the root cause of the disconnection; it cannot be ignored as noise—it is the skeleton of the disconnection timeline.
5. LLM 回傳的 knowledge-core 卡片跳出後，若它有追問（❓ follow-up），可以
   直接在卡片裡回答：「是的，這次是 STA 端主動送出的 TX deauth
   （`hPolicyHandleEvTxDeauthReqFill`），不是 AP 端踢人；reason=0x1 是通用
   代碼，log 裡沒有更明確的觸發原因可考證」——這句話刻意包含一個「no
   log evidence」的坦白，用來測試 Readiness 面板會不會正確把它標成
   `asserted`（而非 `verified`）。

### Step 4 — 針對 DeAuth 本體再補一筆教學
在 Steps 面板找到（或用聊天框，Tag 選 `All` 或對應那個 filter 的
`#N`）輸入：

"DEAUTH_REQ sent to ASUS_AXE11000_6G (7C:10:C9:69:EF:A8), channel=37, band=6_7GHz — This is the only AP that can be connected, so there are no roaming candidates after this deauth, and the device can only rescan (`SCAN_REQUEST`) to find the same AP and connect back."

### Step 5 — 針對週期性 AP 評分教學（可選，加強 expert_rules 深度）
在 `ApSelectionApply` 那個 filter 的 🎓：

"`prvApSelectionApplyGradeBonuses` / `prvApSelectionChooseBestCandidate`

is the AP rating string actually printed by the current firmware. The old keyword `candidate grade`

is not found (stale wording, can be disabled). This log only shows a single AP. The rating is run every 2–6

minutes as a background health check, not indicating roaming is imminent; the grade dropped from 1348620 to 345840, mainly due to chLoad increasing from 35 to 80 (channel congestion). RSSI only dropped slightly from -56 to -57. This can be used as a reference for later determining whether the 'grade drop is due to congestion or a signal'."

### Step 6 — 確認 `PROP_SET_` 排除正確（不用動，作為對照）
可以在 Steps 面板用 ❓（若已還原此按鈕）或直接在聊天框問一句「PROP_SET_
系列排除得對嗎？」，預期得到「對，這些是連線建立的例行 OID 設定回報，跟
deauth 根因無關」——用來驗證系統不會為了「找碴」而亂建議修改正確的判斷。

### Step 7 — Log Round & Analyze
點 **Log Round & Analyze**，觀察：
- Readiness 分數應隨著上面幾筆教學上升
- Coverage 的 `knowledge` 子分數應反映 expert_rules 有實質內容
- Validation 清單裡，Step 3 提到的「STA 主動 deauth、reason=0x1 無法進一步
  考證」那句應該被標成 `asserted`，不是 `verified`（這是測試「防呆」機制
  有沒有正常運作的關鍵檢查點）

### Step 8 — Export（建議用這個場景測 Phase 1+2+3 的 AutoSkill 管線）
1. 先在 **skillSelect** 下拉選單載入既有的 `connection_flow` skill 當篩選
   基準（`loadSkill`）→ 這會把 `state.active_skill_key` 設成
   `connection_flow`，並自動切到 **PRIOR** 模式。
2. 點 **Export Skill**。我用這次教學內容模擬了一份合理的 draft，離線跑
   `skill_retrieval.score_against()` против 本地 `connection_flow`（keywords
   本來就重疊 `CNCT_FLOW`/`DeAuth`/`ApSelectionApply`/`Best Candidate` 等），
   算出相似度 **0.43**——會通過 Tier 0 的下限（0.15），但不到「直接跳過
   LLM 判斷、強制合併」的門檻（0.55）。也就是說 `route_draft` **會**呼叫
   Agent B 的 judge 去確認是不是同一能力，不是無條件合併。名稱同樣是
   "Connection Flow"、關鍵字大量重疊，正常情況下 judge 應該會回
   `merge`；但如果那次 LLM 呼叫失敗、逾時、或回傳無法解析，`judge_candidate`
   會 fail-closed 回 `add`（這是刻意設計的安全機制，不是 bug）——所以
   Edit-Skill modal 標題「**Merge into Existing Skill**」是**預期最可能**
   的結果，不是保證值；如果這次看到的是「New Skill」，先檢查 LLM 有沒有
   正常回應，而不是急著當作程式壞了。
3. 檢查 modal 裡的綠色高亮：`stateMachineSetStateNoCurrentFlow` 這個新
   include 應該出現在綠色 diff 裡（因為它在這次 filter run 裡
   `unique_hits > 0`，不會被 Phase 3 的數據驗證擋下來）；如果你剛好也把
   某個 0-hit 的舊關鍵字重新啟用又沒有效果，它應該出現在「未自動加入」的
   提示清單，而不是被靜默塞進 keywords。
4. 確認 modal subtitle 顯示 `connection_flow (existing, v0.x.x)`，Save
   後回到 Skill Library 檢查版本號真的 +1、`version_history` 多了一筆
   存檔前快照。

---

## 預期結果檢查表

- [ ] Step 1：Filtered Log 首次套用，14 個關鍵字 Hits 顯示 0
- [ ] Step 3：取消勾選 exclude + 新增 include 後，5 筆斷線狀態機行重新出現
      在 Filtered Log
- [ ] Step 3-4：Readiness 的 Validation 清單出現至少一筆 `asserted`（deauth
      觸發原因無法從 log 進一步考證）
- [ ] Step 8：Export 時 Edit-Skill modal 顯示「Merge into Existing Skill:
      connection_flow」而非「New Skill」
- [ ] Step 8：`stateMachineSetStateNoCurrentFlow` 在 diff 裡以綠色 NEW 呈現
- [ ] Step 8：存檔後 `connection_flow` 版本號遞增，`version_history` 有
      一筆新快照，且舊的 keywords/expert_rules 沒有被覆蓋消失
