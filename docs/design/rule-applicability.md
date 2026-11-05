# 規則的「適用對象」與「要求」分離：設計說明

狀態：提案，尚未實作。本文件不改變任何現行行為。

## 目的

一條規則回答兩個不同的問題：

1. **適用對象（applicability）**：這條規則要檢查哪些物件？例如「SCADA 系統、位於機房區域內的纜線」。
2. **要求（requirement）**：被選中的物件必須滿足什麼？例如「與電力纜線的水平淨距 >= 300 mm」。

這個分法參考 buildingSMART IDS（IfcTester 實作的標準）：先用 applicability 選出物件群，再對群中每個物件檢查 requirements。
本專案只借用這個概念，不引入 IfcTester（它處理 IFC，授權為 LGPL-3.0-or-later）。

分開之後，報告才能清楚區分三種完全不同的結果：

| 結果 | 意義 | 使用者該做的事 |
|---|---|---|
| 不適用 | 圖面上沒有這條規則要檢查的物件 | 確認選取條件（圖層、系統）是否寫對，或確認本圖確實不含此系統 |
| 證據不足 | 有適用物件，但圖面缺少判定所需的資料（例如 Z 高程、目標物件、區域） | 補資料或人工判定 |
| 未達要求 | 有適用物件、資料足夠，量測值不符合要求 | 修改設計 |

## 現況

規則格式（`src/core/rules/schema.py`）其實已經把兩者分在不同欄位：

- 適用對象：`subject`、`target`、`zone`（實體條件）與 `pair_filter`（配對條件），由 `core.rules.predicate` 判斷。
- 要求：`measurement`、`operator`、`value`、`unit`、`warn_margin`、`quantifier`。
- 來源：`source`、`evidence_id`／`evidence_ref`；`apply_confidence` 依證據給 `CONFIRMED`／`INFERRED`／`UNKNOWN`。

問題不在欄位，而在**結果沒有把「不適用」與「證據不足」分開**，而且各評估路徑的處理不一致：

| 情況 | 目前的行為 | 位置 |
|---|---|---|
| 沒有符合 `subject` 的物件（距離、交叉、重疊、區域、長度、角度） | 不產生任何結果；`rule_stats.execution = "skipped"`，`skip_reason = "no matching subject entities"` | `eval_pairs`／`eval_zone`／`eval_single`、`pipeline.run_analysis` |
| 沒有符合 `subject` 的物件（`connectivity`） | 產生一筆 `UNKNOWN`，理由「Insufficient evidence：圖面沒有符合 subject selector 的圖元」 | `eval_connectivity` |
| 沒有符合 `subject` 的物件（`entity_count`／`entity_exists`） | 以數量 0 判定，可能是 `FAIL` 或 `PASS`；這是要求本身，行為正確 | `eval_count` |
| 有 subject，但圖面沒有符合 `target` 的物件 | 每個 subject 一筆 `UNKNOWN`，理由「Insufficient evidence：找不到目標物件」 | `eval_pairs` |
| 有 subject，但找不到 `zone` | 每個 subject 一筆 `UNKNOWN`，理由「找不到區域圖元」 | `eval_zone` |
| 平面交疊但缺 Z（水平淨距）、缺 Z（垂直淨距） | `UNKNOWN`，理由為 Insufficient evidence | `pair_value` |
| 物件沒有可量測的方向（角度規則遇到圓） | `UNKNOWN` | `eval_single` |

由此產生三個具體問題：

1. **同一件事有兩種答案。** 沒有適用物件時，`connectivity` 報 `UNKNOWN`，其他規則靜默略過。
2. **「找不到目標」被歸為證據不足。** 圖面缺少某系統的目標物件，可能代表「本圖不含電力纜線，所以不適用」，也可能代表「圖層命名錯誤」。目前一律以 `UNKNOWN` 混在缺 Z 之類真正的資料不足裡，報告無法分開統計。
3. **覆蓋率的分母名稱與意義不符。** `coverage.applicable` 是「已啟用的規則數」，不是「有適用對象的規則數」；`skipped` 混合了「不適用」與其他原因。`coverage_notice` 以此判斷是否「完整覆蓋」。

另外，人工確認目前只在規範擷取的候選規則上（`RuleCandidate.confirmed_by`），而 `apply_confidence` 對每筆結果都附註「適用對象與方向仍需人工確認」，規則本身沒有記錄「適用對象已確認」。

## 提案

### 1. 規則格式：維持現有欄位，增加一個可選的適用性設定

不搬動 `subject`／`target`／`zone`，避免既有規則檔、範本與規範擷取全部改寫。新增一個可選欄位：

```json
{
  "id": "SCADA-CLEARANCE-H-300",
  "subject": {"system": "SCADA"},
  "target": {"system": "POWER"},
  "applicability": {"subject": "optional", "target": "optional"},
  "measurement": "horizontal_clearance", "operator": ">=", "value": 300, "unit": "mm"
}
```

可用值沿用 IDS 的 cardinality 用語：

| 值 | 物件群為空時 | 物件群不為空時 |
|---|---|---|
| `optional`（預設） | 結果為「不適用」 | 照常檢查要求 |
| `required` | 結果為「未達要求」（例如本圖應有接地線卻沒有） | 照常檢查要求 |
| `prohibited` | 通過 | 每個物件一筆「未達要求」（例如不得出現某圖層） |

`prohibited` 與現有的 `entity_count == 0` 重疊。實作時二擇一，避免同一件事有兩種寫法；建議保留 `entity_count`，`prohibited` 先不開放。

`applicability.target` 只對配對量測（`PAIR_MEASUREMENTS`）有意義；`applicability.zone` 只對區域量測有意義。`normalize_rule` 對不適用的鍵報錯。

### 2. 結果：不新增狀態，在 details 中標示 UNKNOWN 的種類

`RuleResult.status` 的四種值（`FAIL`／`WARNING`／`PASS`／`UNKNOWN`）被 issue ID、匯出（CSV、Markdown、HTML、DXF 標註）、介面與大量測試依賴。新增狀態的成本高，因此：

- 「不適用」**不產生 issue**，記錄在 `rule_stats`：`execution = "not_applicable"`，並寫明是哪個物件群為空（`subject`／`target`／`zone`）。
- 「證據不足」維持 `status = "UNKNOWN"`，新增 `details.unknown_kind`，取值：
  `no_elevation`、`unsupported_geometry`、`too_few_entities`、`no_target`、`no_zone`、`no_subject`。
  後三者只在對應物件群設為 `required` 時才會以 `UNKNOWN` 出現。
- 「未達要求」維持 `FAIL`／`WARNING`。

`connectivity` 改為與其他規則一致：沒有適用物件且為 `optional` 時，記為「不適用」，不再產生 `UNKNOWN`。

### 3. 覆蓋率：分母改為真正有適用對象的規則

```text
loaded        已載入的規則
enabled       已啟用的規則（現在的 applicable）
not_applicable 已啟用、但本圖沒有適用對象的規則
applicable    enabled - not_applicable
evaluated     applicable 中有產生結果的規則
```

`coverage_notice` 判斷「完整覆蓋」時使用新的 `applicable`，並在報告中另列「本圖不適用的規則」清單與原因，讓使用者能發現選取條件寫錯的規則。

### 4. 人工確認：記錄在規則上，而不是只寫在每筆結果的說明裡

在規則增加可選的 `review`：

```json
"review": {"applicability": "confirmed", "requirement": "confirmed", "by": "user", "at": "2026-10-15"}
```

`apply_confidence` 依此決定是否仍附註「適用對象與方向仍需人工確認」。規範擷取確認候選規則時（`validator`）一併寫入。

## 相容性

- 沒有 `applicability` 的既有規則行為：subject 與 zone 視為 `optional`（與現在一致：沒有 subject 即略過）。
  **target 是唯一的行為變更點**：現在找不到 target 會產生 `UNKNOWN`；若預設 `optional`，這些結果會消失，改列為「不適用」。
  要完全保留現有結果，target 的預設需為 `required`。建議預設 `required`，讓既有報告不變，新規則再明確選擇。
- `connectivity` 沒有 subject 時從 `UNKNOWN` 改為「不適用」，會讓既有報告少一筆 `UNKNOWN`。
- 覆蓋率欄位改名會影響 `summary.coverage` 的 JSON、HTML 報告卡片與 Markdown 報告；`applicable` 的數值意義改變，舊專案重新分析後數字可能不同。
- 規則檔 `version` 維持 1（只新增可選欄位）；若 target 預設改為 `optional`，則應升為 2 並提供轉換。

## 測試計畫

- 每種量測 × `optional`／`required` × 物件群為空／不為空，驗證結果、`rule_stats.execution` 與 `unknown_kind`。
- 既有的 golden hash（`tests/regression/test_all_within_parity.py`）在預設設定下必須不變，證明預設行為相容。
- 覆蓋率：構造「部分規則不適用」的圖面，驗證 `not_applicable`、`applicable`、`coverage_notice` 與報告中的清單。
- `connectivity` 與其他規則在沒有 subject 時的行為一致。
- `normalize_rule`：在非配對量測上設定 `applicability.target` 等不適用的鍵要報錯。

## 待決定事項

1. `target` 的預設：`required`（保留現有報告，建議）或 `optional`（語意較乾淨，但會改變既有結果）。
2. 覆蓋率欄位是直接改名，還是保留 `applicable` 舊意義並新增欄位，讓舊報告格式不變。
3. 是否開放 `prohibited`，或繼續只用 `entity_count == 0`。
4. `review` 是否也要在介面上編輯，或只由規範擷取流程寫入。
