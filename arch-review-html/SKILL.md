---
name: arch-review-html
description: 為 C/C++ 的 module、daemon 或 AP（例如 IPACM、ipa_mon、QCMAP、netmgr、任何 userspace 服務或 kernel 模組）產生「互動式架構流程網頁」做 code review：一份給使用者看的自包含 HTML（架構圖、可多層展開到函式 / case、流程逐步播放、點區塊顯示對應源碼、坑與板上驗證），加上一份給 Claude 對照用的 JSON（區塊 → 檔案:行號）。只要使用者想用圖 / 網頁 / HTML 來 review、理解或導覽某包源碼的架構、流程、元件關係，或提到「架構流程互動網頁」、「review HTML」、「點區塊看源碼」、「展開內層」、「review-task」，就使用這個技能，即使沒有明說「網頁」。不適用於撰寫 mydoc 下的 driver 解說文件（那是另一套文件規範）。
---

# 架構 review 網頁（arch-review-html）

為一包 C/C++ 源碼產生兩個檔案，放在同一個目錄：

| 檔案 | 給誰 | 內容 |
|---|---|---|
| `<name>.json` | Claude | 唯一的事實來源：區塊、行號範圍、錨點、邊、流程、坑、驗證、子層、頂層排版 |
| `<name>.html` | 使用者 | 由 `scripts/build_arch.py` 從 JSON + 源碼產生的單一檔案網頁，離線可開 |

使用者之後說「某某區塊」，Claude 就查 JSON 找到 file:line 回到源碼。所以 JSON 的正確性比網頁外觀重要。

## 工具（都在本技能的 `scripts/`，直接以絕對路徑執行，不要複製進專案）

- `build_arch.py SPEC.json …`：驗證錨點 / 範圍 / 函式 / case，解析子層、自動產生呼叫邊，輸出 `SPEC.html`。任何一項對不上就 exit 1 且不輸出。
  - `--fill-anchors SPEC.json`：把寫成 `{"line": N}` 的錨點補上該行原文（保留 JSON 排版）。
  - `--dump SPEC.json`：額外列出每個子層節點解析到的行號與呼叫邊，新增子層後一定要看。
- `fnmap.py FILE name… [-- regex…]`：列出函式定義範圍、彼此呼叫、函式內是否出現指定 regex。定位規則與 build_arch.py 相同，NOT FOUND 代表 build 也會失敗。
- `cases.py FILE START END`：列出 switch 每個 case 的範圍、呼叫了哪些 handler、送了哪些事件。

## 流程

### 1. 確認範圍（不清楚才問）

- 主題與源碼位置；**邊界**（例如「只到 userspace」：kernel / 外部函式庫只當邊界節點，不往裡追）。
- 輸出目錄與檔名。使用者沒指定時，放在他上次放 review 網頁的目錄（之前用的是 `src/mydoc/<主題>/review-task/`），檔名 `<主題>_arch.json/html`。
- 相對路徑寫在 JSON 裡（`"source"` 或 `"root"`），以 JSON 所在目錄為基準。

### 2. 讀源碼建立架構模型

先找：進入點與執行緒、事件 / 訊息從哪來、核心資料結構、對外呼叫（ioctl、socket、函式庫）。大型 codebase 用 grep 找函式頭與 `pthread_create`、`case`、`PostEvt` 之類的匯流點，再用 `fnmap.py` / `cases.py` 看結構，最後讀關鍵段落。

內容規則（這些是使用者的明確要求）：
- 只寫實際讀過的源碼能支持的內容；推論或未驗證的部分標「推測」。
- 「坑」只寫能從程式碼證明的陷阱（附行號），不寫泛用通則。
- 「驗證」只寫能在板上執行、且從程式碼推得預期結果的指令。
- 沒讀的部分（例如某元件只追到入口）在說明裡明講。

### 3. 寫 JSON

格式見 `references/spec-format.md`（必讀）。要點：
- 頂層區塊 15–35 個為宜：初始化、事件來源 / 執行緒、核心處理、對外邊界、共用定義。
- 每個區塊有 `ranges`、`anchor: {"line": N}`（之後用 `--fill-anchors` 補文字）、`summary`、`details`、`highlight`。
- `flows`：3–7 條，每條是一個真實時序（啟動、一個事件的路徑、典型使用情境、關閉）。播放時流程會首尾相接，所以按「啟動 → 執行期 → 結束」的順序排。
- `pitfalls`、`verify` 依上面的內容規則。
- `"view"`：頂層排版，見 `references/layout-guide.md`。

### 4. 為重要元件加子層（多層展開）

使用者 review 時會想「點進去看這個元件內部的介面與功能」。對核心元件加 `sub`：
- 第二層：對外介面（`in`）、內部函式（`fn`）、資料結構（`data`）、外部 / 其他元件（`ref`）。
- 第三層：大型 `event_callback` / `switch` 依 `case` 展開，`ref` 指回第二層的 handler。
- 子層用函式名稱 / case 名稱定位，不寫行號，源碼改了也不必維護。
- 呼叫邊由掃描函式本體產生，是文字比對：同名函式（`IPACM_Config::GetInstance` vs `NatApp::GetInstance`）、`::registr` vs `registr`、`~Foo()` 含 `Foo(`、以 callback 指標傳遞的函式都要用 `match` 修正或補 `edges`。`#ifdef` 關掉的程式碼也會被掃到。
- 每次加完跑 `--dump`，逐一看呼叫邊是否合理。

### 5. 建置與檢查

1. `python3 <skill>/scripts/build_arch.py --fill-anchors SPEC.json`
2. `python3 <skill>/scripts/build_arch.py --dump SPEC.json`，修正所有錯誤。
3. 抽查：用小段 Python 印出每條坑、每個流程步驟引用的行，確認真的指到描述的程式碼。
4. 環境沒有瀏覽器時，網頁外觀無法實際確認，回報時要明說。若需要驗證互動，可在 scratchpad 安裝 jsdom 寫簡單測試，不要裝進專案目錄。

### 6. 回報

先說產出位置與結論，再列：架構圖包含哪些層與區塊、流程、重點坑（附 file:line 連結）、標「推測」的項目、沒讀到的部分。用繁體中文。

## 維護既有頁面

- 源碼改了：直接重跑 build。錨點失敗會列出哪個區塊漂移，依函式名稱重新定位並更新 JSON 的行號，再 `--fill-anchors`。
- 只有函式改名：子層的 `fn` 名稱與 JSON 說明文字裡的名稱都要換；注意 JSON 字串裡 `\t` 開頭的錨點，`\b` 比對不到。
- 使用者要求介面調整：改 `scripts/build_arch.py` 裡的 `TEMPLATE`，所有主題重建後都會生效。

## 網頁功能（回答使用者時可引用）

點區塊顯示說明 / 坑 / 源碼並高亮重點行；⊞ 區塊可雙擊或按「展開內部」進入下一層，麵包屑或 Backspace 回上層；ref 節點可「前往」跨層跳轉；流程按鈕逐步播放（← →、空白鍵、Esc），流程尾端接下一條；完整源碼分頁可選檔並點行反查最深的節點；搜尋函式 / case 名稱；中間分隔線可拖曳（雙擊還原、記住寬度）；深淺色切換。
