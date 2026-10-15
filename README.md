# tw-stock-data

每天自動從 [FinMind](https://finmindtrade.com/) 抓台股資料，存成 CSV，給 Claude 的紙上交易日報／週報使用。

## 資料

| 檔案 | 內容 |
|---|---|
| `data/latest.csv` | 每檔最新一天：收盤價、漲跌幅、成交量、三大法人買賣超 |
| `data/prices/<代號>.csv` | 每日股價（2015 起）：date,open,high,low,close,volume |
| `data/inst/<代號>.csv` | 三大法人買賣超（2018 起，單位：股）：date,foreign_net,trust_net,dealer_net |
| `data/revenue/<代號>.csv` | 月營收（2015 起）：date,revenue_year,revenue_month,revenue |
| `data/meta.json` | 最後更新時間、最新交易日、這次失敗的代號 |

`TAIEX` 是加權指數；ETF 與指數沒有法人與營收資料。

## 自動執行

`.github/workflows/fetch.yml`：台北時間週一～五 16:10 與 07:20 各跑一次，也可以在 **Actions → 抓台股資料 → Run workflow** 手動執行。

## 新增追蹤的股票

編輯 `tickers.txt`，一行一個代號，存檔後下次執行就會自動補抓完整歷史。

## 設定

FinMind token 存在 **Settings → Secrets and variables → Actions** 的 `FINMIND_TOKEN`，不會出現在程式碼裡。

> 資料僅供紙上交易練習與研究，非投資建議。
