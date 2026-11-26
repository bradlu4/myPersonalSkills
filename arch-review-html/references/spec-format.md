# JSON spec 格式

一份 spec 描述一個主題。`build_arch.py` 讀它、驗證、產生同名 `.html`。

## 目錄

1. 頂層欄位
2. 來源檔設定（單檔 / 多檔）
3. 區塊 `blocks[]`
4. 行號參照的寫法
5. 邊 `edges[]`
6. 流程 `flows[]`
7. 坑 `pitfalls[]` 與驗證 `verify[]`
8. 子層 `sub`
9. 最小範例

## 1. 頂層欄位

| 欄位 | 必要 | 說明 |
|---|---|---|
| `doc` | 建議 | 給 Claude 的說明：這份 JSON 怎麼用、邊界、行號漂移怎麼處理 |
| `title` | 是 | 網頁標題，例如 `"IPACM 架構流程"` |
| `source` / `root` | 二擇一 | 見第 2 節 |
| `generated` | 建議 | 產生日期 `YYYY-MM-DD` |
| `categories` | 是 | `{key: 中文說明}`，區塊依 key 上色。`ext` 固定灰色（邊界）、`support` 固定土黃色，其他依出現順序配色 |
| `blocks` | 是 | 第 3 節 |
| `edges` | 是 | 第 5 節 |
| `flows` | 是 | 第 6 節 |
| `pitfalls`, `verify` | 建議 | 第 7 節 |
| `output_schema` 等 | 自由 | 任何對 Claude 有用的額外對照資料都可以放，網頁不會用到 |
| `view` | 是 | 頂層排版，見 `layout-guide.md` |

## 2. 來源檔設定

- 單檔：`"source": "../ipa_mon.c"`。所有行號都指這個檔。
- 多檔：`"root": "../../../data-ipa-cfg-mgr/ipacm"`，每個區塊用 `"file": "src/IPACM_Main.cpp"` 指定主要檔案。

路徑都相對於 JSON 所在目錄。只有被引用到的檔案會嵌入網頁（完整源碼分頁可切換這些檔案）。

## 3. 區塊

```json
{
  "id": "post_evt",
  "title": "PostEvt()",
  "category": "bus",
  "file": "src/IPACM_EvtDispatcher.cpp",
  "ranges": [[57, 120], ["inc/IPACM_CmdQueue.h", 52, 106]],
  "anchor": {"line": 57},
  "symbols": ["IPACM_EvtDispatcher::PostEvt"],
  "summary": "一句話說明它負責什麼。",
  "details": ["重點 1", "重點 2"],
  "highlight": [65, 68, "inc/IPACM_CmdQueue.h:59"],
  "external": "（只有 ext 類別需要）邊界的實際對象，例如 /dev/ipa ioctl",
  "sub": { }
}
```

- `ranges`：這個區塊對應的程式碼片段，可多段、可跨檔。完整源碼分頁「點行反查區塊」用的是最小的範圍，所以範圍盡量精準。
- `anchor`：先寫 `{"line": N}`（可加 `"file"`），再跑 `--fill-anchors` 補上 `"text"`。之後每次 build 都會比對該行原文，用來偵測行號漂移。
- `symbols`：搜尋框可以搜到的名稱。
- `highlight`：區塊源碼中要標黃的重點行。

## 4. 行號參照的寫法

`highlight`、流程步驟的 `lines`、坑的 `lines` 都接受：
- 整數：屬於該區塊（或該步驟區塊）的 `file`。
- `"相對路徑:行號"`：指定其他檔案。

## 5. 邊

```json
{"from": "wan", "to": "post_evt", "kind": "call", "label": "回授事件", "both": false}
```

`kind`：`call`（實線）、`control`（實線）、`data`（虛線）、`inherit`（點線）。`both: true` 時兩端都有箭頭。路線由 `view.routes` 指定，或自動產生（見 `layout-guide.md`）。

## 6. 流程

```json
{"id": "wan_up", "title": "WAN 上線", "steps": [
  {"block": "th_netlink", "lines": [1111, 1115], "note": "IFF_UP → IPA_LINK_UP_EVENT。"}
]}
```

- 只能引用頂層區塊。播放時相鄰兩步之間若有邊就會亮起來，所以步驟順序盡量沿著真實的邊。
- `lines` 會以深黃色標示並捲動到第一行；空陣列代表只亮區塊（例如邊界節點）。

## 7. 坑與驗證

```json
{"blocks": ["cfg", "main"], "lines": ["src/IPACM_Iface.cpp:64", "src/IPACM_Main.cpp:1790"], "text": "…"}
{"text": "預期看到…", "cmd": "for t in /proc/$(pidof ipacm)/task/*; do cat $t/comm; done"}
```

坑會出現在 `blocks` 列出的區塊資訊裡，也會出現在 `lines` 落在其範圍內的子層節點。標「推測」的坑請在 `text` 開頭寫明。

## 8. 子層 `sub`

```json
"sub": {
  "nodes": [
    {"id": "AddEntry", "kind": "in", "fn": "NatApp::AddEntry", "label": "新增一筆 NAT 規則", "summary": "…", "callers": ["ctl"]},
    {"id": "GetInstance", "kind": "in", "fn": "NatApp::GetInstance", "match": ["NatApp::GetInstance\\s*\\("]},
    {"id": "cache", "kind": "data", "title": "cache[]", "file": "inc/X.h", "ranges": [[679, 679]], "match": ["\\bcache\\["]},
    {"id": "ipanat", "kind": "ref", "ref": "x_ipanat", "match": ["\\bipa_nat_add_ipv4_rule\\s*\\("]},
    {"id": "event_callback", "kind": "in", "fn": "Foo::event_callback", "sub": {"nodes": [
      {"id": "c_up", "kind": "case", "case": "IPA_LINK_UP_EVENT", "summary": "…"},
      {"id": "r_handle", "kind": "ref", "ref": "foo.handle_up"}
    ]}}
  ],
  "edges": [{"from": "listener_start", "to": "recv_msg", "label": "read_func"}]
}
```

| 欄位 | 說明 |
|---|---|
| `id` | 同層內唯一。完整 id 會自動加上父層前綴：`nat.AddEntry`、`lan.event_callback.c_up` |
| `kind` | `in` 對外介面、`fn` 內部函式、`case` 事件分支、`data` 資料、`ref` 指向其他節點 |
| `fn` | 函式名稱（`Class::name` 或 `name`），可加 `nth` 選第幾個定義。範圍＝定義行到第 0 欄的 `}` |
| `case` | case 標籤；在父節點的函式範圍內找（或用 `within` 指定函式）。空的 fall-through case 會併到下一個 |
| `file` | 預設繼承父節點 |
| `ranges` | 只有 `data` 或特殊情況才直接寫行號 |
| `ref` | 目標的完整 id（可以是頂層區塊或任何子層節點）。網頁上雙擊或按「前往」會跳過去 |
| `match` | 比對規則（regex 陣列）。其他節點的本體出現這些字串時，就產生一條指向本節點的邊。沒寫時：`fn` 用 `\b短名\s*\(`；`ref` 沿用目標的 `match` 或 `fn` |
| `label` | 方塊第二行的短說明（約 15 個中文字內） |
| `callers` | 從外部呼叫它的頂層區塊 id，顯示為「被呼叫自」 |
| `title` | 預設為 `短名()` 或 case 名稱 |

`edges` 只需寫自動掃描抓不到的邊（例如函式指標）；`from` / `to` 用同層的短 id。

## 9. 最小範例

```json
{
  "title": "demo 架構流程",
  "source": "../demo.c",
  "generated": "2026-11-25",
  "categories": {"init": "初始化", "loop": "主迴圈", "ext": "邊界"},
  "blocks": [
    {"id": "main", "title": "main()", "category": "init", "ranges": [[10, 40]], "anchor": {"line": 10},
     "symbols": ["main"], "summary": "解析參數並進入迴圈。", "details": [], "highlight": [12]},
    {"id": "loop", "title": "主迴圈", "category": "loop", "ranges": [[42, 80]], "anchor": {"line": 42},
     "symbols": ["run_loop"], "summary": "…", "details": [], "highlight": []},
    {"id": "x_dev", "title": "/dev/foo", "category": "ext", "external": "foo driver", "ranges": [[55, 55]],
     "anchor": {"line": 55}, "symbols": [], "summary": "…", "details": [], "highlight": []}
  ],
  "edges": [{"from": "main", "to": "loop", "kind": "control"}, {"from": "loop", "to": "x_dev", "kind": "data", "label": "read()"}],
  "flows": [{"id": "start", "title": "啟動", "steps": [{"block": "main", "lines": [12], "note": "…"}, {"block": "loop", "lines": [], "note": "…"}]}],
  "pitfalls": [],
  "view": {"w": 700, "h": 260, "layout": {
    "main": [20, 30, 180, 54, ["解析參數"]],
    "loop": [260, 30, 180, 54, ["select 迴圈"]],
    "x_dev": [500, 30, 170, 54, ["foo driver"]]
  }}
}
```
