# alpha-mining

台股研究工作台：用公式描述交易訊號（alpha），評估預測力，轉成策略回測，並在類似
TradingView 的 K 線介面上檢視結果。目前支援現貨（如 0050、個股），台指選擇權開發中。

設計原則：

- **不偷看未來**：訊號在第 t 日收盤後產生、第 t+1 日開盤成交；所有公式函數只用當天（含）以前的資料，
  並以測試逐一驗證。
- **帳可以對**：會計以 `Decimal` 計算，每天核對「現金 + 股數 × 收盤價 = 淨值」。
- **資料可追溯**：保留原始回應與 SHA-256；清理後資料與回測報告都記錄來源與雜湊。
- **不悄悄出錯**：引擎還不會處理的公司行動（配股、減資、反分割）會讓匯入直接停止。

## 安裝與啟動

需要 Python 3.12+，執行期只用標準函式庫（Windows 若缺時區資料需 `pip install tzdata`）。

```powershell
$env:PYTHONPATH = "src"
python -m finback                       # 開啟工作台 http://127.0.0.1:8050/
```

第一次使用時在工作台按「＋ 下載」從 FinMind 抓日線，或用指令：

```powershell
python -m finback.finmind 0050 --start 2020-01-01 --end 2025-12-31
```

FinMind 免 token 即可使用但有流量上限；設定環境變數 `FINMIND_TOKEN` 可提高上限（token 不會寫入任何檔案）。

## 工作台

- **K 線圖**：TradingView 開源的 [Lightweight Charts](https://github.com/tradingview/lightweight-charts)
  （Apache-2.0，已放在 `src/finback/web/vendor/`，離線可用）。台股慣例紅漲綠跌；價格做分割調整（僅顯示用），
  圖上標出除息、分割、樣本外起點與每筆買賣。窗格：價格與成交量／alpha／淨值（策略 vs 買入持有）。
- **Alpha 池**（`configs/alphas.json`）：每個 alpha 是一條公式，例如
  `ts_mean(close, 20) / ts_mean(close, 60) - 1`，可引用池內其他 alpha 的名稱。
  公式以白名單語法解析，不執行任意程式碼。函數清單在介面的「函數與變數」。
- **評估**：IC、Rank IC、勝率、分組報酬，預測期 1／5／20 日。樣本內／樣本外分開計算；
  分組界線只用樣本內資料，樣本內也排除報酬區間跨進樣本外的資料。
- **策略回測**：alpha 門檻（> 進場門檻買進、< 出場門檻賣出）或內建策略，成本、本金、交易單位可調；
  結果分樣本內／外並與買入持有比較，可另存成報告（`/reports` 頁面檢視）。

**避免過度擬合**：調整公式與門檻時只看樣本內；樣本外留到最後檢驗，看過越多次就越不「外」。

## 用設定檔批次回測

```powershell
python -m finback.backtest configs/strategies/0050_trend_2020_2025.json
```

設定檔指定商品、期間、本金、成本與多個策略，結果寫到 `reports/<name>/`（每日帳本、成交明細、`run.json`）。

| type | 參數 | 規則 |
|---|---|---|
| `alpha` | `expr`, `entry`, `exit`, `weight` | alpha > entry 持有、< exit 空手；可用 alpha 池中的名稱 |
| `buy_and_hold` | `weight` | 首日開盤買入後不再交易 |
| `fixed_weight` | `weight`, `period`（month/quarter） | 每期第一個交易日收盤後再平衡 |
| `sma_cross` | `fast`, `slow`, `weight` | 快線 > 慢線持有，否則空手 |
| `momentum` | `lookback`, `weight` | 收盤高於 N 日前收盤時持有 |

新增策略：在 [strategies.py](src/finback/strategies.py) 寫一個有 `target_weight(history) -> Decimal | None`
的類別並登記到 `STRATEGIES`（回傳 `None` 表示不動，0~1 表示隔日開盤調整到該比例）。

## 回測假設

- **成本**：手續費 = max(成交金額 × 費率 × 折扣, 最低手續費)，賣出另收交易稅，皆無條件捨去至元。
  預設法定上限 0.1425%、最低 20 元；交易稅 ETF 0.1%、一般股票 0.3%。皆為輸入假設，非券商報價。
- **公司行動**：現金股利於除息日入帳（給除息前已持有的股數），整數分割於開盤前調整股數。
- **訊號用價格**：以前向還原（只用過去事件）的含息價格計算，分割與除息不會變成假跌幅。
- **尚未模擬**：滑價、漲跌停與成交量限制、現金利息、期末賣出成本、股利再投入。

## 專案結構

```text
src/finback/
  alpha.py        # alpha 公式語言、前向還原價、預測力評估
  engine.py       # 回測引擎（訊號、成交、成本、分割與股利）
  strategies.py   # 內建策略與 alpha 門檻策略
  prices.py       # 日線資料列、帳本快照、共用統計
  finmind.py      # FinMind 下載與公司行動檢查
  workbench.py    # 工作台 API（資料、alpha 池、評估、回測）
  dashboard.py    # 本機伺服器（只綁定 127.0.0.1）
  backtest.py     # 設定檔批次回測 CLI
  domain/         # 商品、訂單、成交模型（期貨／選擇權共用）
  portfolio/      # 現金、部位與損益
  web/            # 工作台與報告頁前端；vendor/ 為 Lightweight Charts
configs/          # alpha 池與策略設定
tests/            # 單元測試（不需網路）
data/, reports/   # 本機資料與結果，不進版控
```

## 測試

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## 路線圖

1. ✅ 現貨回測引擎、FinMind 資料、alpha 公式池、工作台
2. ⏳ 台指選擇權（TXO）：契約鏈資料、依 alpha 訊號買進買權／賣權、權利金與到期結算
3. 樣本外與滾動前進（walk-forward）驗證流程
4. 小額實盤／紙上交易紀錄比對
