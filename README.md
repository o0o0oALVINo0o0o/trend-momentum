# trend-momentum

**A pre-registered, one-shot Stage 0 backtest of time-series momentum on Binance USDⓈ-M perpetuals. Result: no evidence of an edge.**
（繁體中文說明在下方 / Traditional Chinese below）

> **Not investment advice.** This is a backtest, never traded live. It contains no signals, no strategy to follow and no account or API-key handling. Fee, slippage and funding assumptions are stated in `SPEC.md`.

## What this is

A research record, not a trading product. The specification (`SPEC.md`) was frozen and hash-registered in `FREEZE.txt` **before** any return was computed, and the formal run was executed **once** (`results.txt`). Three-valued verdicts (PASS / FAIL / UNKNOWN) are used: missing data produces UNKNOWN, never a silent PASS or FAIL.

Setup: sign of the past N-day return (N ∈ {20, 60, 120}), long/short, daily rebalance, execution at the open of t+2, liquid contracts only (30-day mean quote volume ≥ 20M USDT), taker fee 0.05%/side plus tiered slippage and funding, in-sample / out-of-sample cut at 2025-02-15, 922 USDT-margined perpetuals including delisted ones, 2020-01-01 to 2026-10-31.

## Result (see `results.txt`, `CLOSEOUT.md`)

| Item | Number |
|---|---|
| In-sample mean net return per trade, N=20 / 60 / 120 | +0.71% / **+1.22%** (selected) / +0.03% |
| Bootstrap lower bounds for N=60 (three dependence-aware bootstraps) | all negative, so indistinguishable from zero |
| Out-of-sample mean net return per trade (N=60, 5,579 trades) | **−1.53%** (excess vs. random entry −1.73%) |
| Out-of-sample portfolio ending equity | 0.67 |
| Overall verdict | Qualification **UNKNOWN**; the performance conditions that could be evaluated (2, 3, 4, 5b) all **FAIL** |

The UNKNOWN comes from one market-wide missing day (2026-10-28) on the data site, not from the strategy. **This does not show that momentum does not exist in crypto.** Not tested: other holding periods, cross-sectional ranking, volatility sizing, stops, long-only, BTC/ETH alone, intraday data. See `CLOSEOUT.md` §3.

## Reusable parts (standard library only, no third-party packages)

| File | What |
|---|---|
| `fetch.py`, `common.py` | Downloader for the public `data.binance.vision` archive: atomic publish, checksum pairing, strict parsers, coverage flags |
| `core.py` | Daily simulation, stress scenarios S1–S5, full-enumeration random-entry baseline, three bootstraps |
| `analyze.py` | `prep` / `run` stages; `run` refuses to start unless the registered hashes are in `FREEZE.txt` |
| `tests/` | 94 synthetic-data tests including an end-to-end smoke test |
| `docs/DATA_SITE_NOTES.md` | Field notes on the data.binance.vision archive: formats, delisted-contract placeholder rows, funding-interval changes, the missing day (Traditional Chinese) |

## Usage

```
python -m unittest discover -s tests          # tests need no data
python fetch.py list                           # builds the plan (public data only, no keys)
python fetch.py download --workers 8           # about 90k files, ~285 MB with logs
python fetch.py verify
python analyze.py prep
python analyze.py run
```

**Re-running is exploratory only.** The formal result is the single run recorded in `results.txt`; the archive changes over time, so a new run will differ and must not replace it.

## Verifying the frozen hashes

`SPEC_v3_frozen.md`, `SPEC.md` and `results.txt` are hash-locked: any edit, even whitespace or a link fix, breaks the match with `FREEZE.txt`. `.gitattributes` forces LF line endings so the hashes survive a clone on Windows with `core.autocrlf=true`.

```
sha256sum SPEC_v3_frozen.md SPEC.md results.txt
```

`FREEZE.txt` records no third-party timestamp: it documents the order of events only through hashes written during the work. `population.csv`, `termination.csv` and `data/` are not in this repository (regenerate with `fetch.py`).

## Notes

- **Language:** the specification and records are in Traditional Chinese; this README is bilingual.
- **Dead references:** `SPEC*.md` and `CLOSEOUT.md` mention `knowledge/…` files from the original private workspace. They are not included, and these documents are frozen, so the links are left as they were.
- **AI assistance:** written with AI assistance (Claude); the specification and code went through four rounds of independent review by Codex. All decisions on scope and pass criteria were made by the human author.
- **Data:** the archive at data.binance.vision is not redistributed here. Check Binance's terms before using it.
- **License:** MIT.

---

# trend-momentum（繁體中文）

**以預先登記、只跑一次的方式，對 Binance USDⓈ-M 永續合約做的時間序列動能 Stage 0 回測。結論：沒有證據顯示有 edge。**

> **非投資建議。** 這是回測，從未實盤；不含訊號、不含可跟單的策略，也不處理帳戶或 API 金鑰。手續費、滑價與資金費假設見 `SPEC.md`。

## 這是什麼

這是研究紀錄，不是交易產品。規格（`SPEC.md`）在**計算任何報酬之前**就凍結並把 SHA-256 登記在 `FREEZE.txt`，正式 run **只執行一次**（`results.txt`）。判定採三值（PASS／FAIL／UNKNOWN）：資料缺漏會得到 UNKNOWN，不會被悄悄當成 PASS 或 FAIL。

設定：過去 N 日報酬的正負（N 取 20、60、120）做多空、每日再平衡、t+2 開盤成交、只做流動標的（30 日均成交額 ≥ 2,000 萬 USDT）、taker 手續費每邊 0.05% 加分層滑價與資金費，樣本內外切點 2025-02-15，922 個 USDT 永續（含已下市），2020-01-01 至 2026-10-31。

## 結果（見 `results.txt`、`CLOSEOUT.md`）

| 項目 | 數字 |
|---|---|
| 樣本內平均每筆淨報酬，N=20／60／120 | +0.71%／**+1.22%**（選定）／+0.03% |
| N=60 的 bootstrap 下界（三種考慮相依性的方法） | 皆為負，與零無法區分 |
| 樣本外平均每筆淨報酬（N=60，5,579 筆） | **−1.53%**（相對隨機進場的超額 −1.73%） |
| 樣本外組合期末權益 | 0.67 |
| 總判定 | 合格 **UNKNOWN**；可評估的績效條件（2、3、4、5b）皆 **FAIL** |

UNKNOWN 來自資料站 2026-10-28 全市場缺一天，不是策略本身的問題。**這不代表動能在加密市場不存在。** 沒測的有：其他持有期、橫斷面排序、波動率調整部位、止損、只做多、BTC／ETH 單獨、盤中資料；詳見 `CLOSEOUT.md` §3。

## 可重用的部分（只用標準函式庫）

| 檔案 | 內容 |
|---|---|
| `fetch.py`、`common.py` | `data.binance.vision` 公開資料抓取：原子發布、zip 與 CHECKSUM 成對修復、嚴格解析、覆蓋旗標 |
| `core.py` | 每日模擬、壓力情境 S1–S5、全列舉隨機進場基準、三種 bootstrap |
| `analyze.py` | `prep`／`run` 兩階段；`run` 在 `FREEZE.txt` 未登記雜湊時拒絕執行 |
| `tests/` | 94 個合成資料測試，含端到端煙霧測試 |
| `docs/DATA_SITE_NOTES.md` | data.binance.vision 實測筆記：檔案格式、下市合約的占位日、資金費週期變化、全市場缺日 |

## 使用方式

```
python -m unittest discover -s tests          # 測試不需要資料
python fetch.py list                           # 建立下載計畫（只讀公開資料，不需金鑰）
python fetch.py download --workers 8           # 約 9 萬個檔案，含日誌約 285 MB
python fetch.py verify
python analyze.py prep
python analyze.py run
```

**重跑只能當探索性分析。** 正式結果就是 `results.txt` 記錄的那一次；資料站內容會隨時間變動，新的 run 結果會不同，不可取代它。

## 驗證凍結雜湊

`SPEC_v3_frozen.md`、`SPEC.md`、`results.txt` 受雜湊鎖定：任何修改（包含空白或修連結）都會讓它與 `FREEZE.txt` 對不上。`.gitattributes` 強制 LF 換行，確保在 Windows 且 `core.autocrlf=true` 的環境 clone 後雜湊仍吻合。

```
sha256sum SPEC_v3_frozen.md SPEC.md results.txt
```

`FREEZE.txt` 沒有第三方時間戳記：它只靠工作期間寫下的雜湊來記錄先後順序。`population.csv`、`termination.csv` 與 `data/` 不在本倉庫（用 `fetch.py` 重新產生）。

## 說明

- **語言：** 規格與紀錄是繁體中文；本 README 為雙語。
- **失效連結：** `SPEC*.md` 與 `CLOSEOUT.md` 提到原本私有工作區的 `knowledge/…` 檔案，不在本倉庫；這些文件已凍結，所以連結維持原狀。
- **AI 協作：** 本專案在 AI（Claude）協助下完成；規格與程式經過 Codex 四輪獨立審查。範圍與判準的決定由人類作者做出。
- **資料：** 不轉散 data.binance.vision 的資料；使用前請自行確認 Binance 的條款。
- **授權：** MIT。
