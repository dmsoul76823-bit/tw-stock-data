"""每天從 FinMind 抓台股資料，存成 CSV。

由 GitHub Actions 自動執行（見 .github/workflows/fetch.yml）。
需要環境變數 FINMIND_TOKEN（存在 GitHub Secrets）。

產出：
  data/prices/<代號>.csv   每日股價：date,open,high,low,close,volume
  data/inst/<代號>.csv     三大法人買賣超（股）：date,foreign_net,trust_net,dealer_net
  data/revenue/<代號>.csv  月營收：date,revenue_year,revenue_month,revenue
  data/latest.csv          每檔最新一天的摘要（日報／週報直接讀這份）
  data/meta.json           更新時間、最新交易日、失敗清單
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://api.finmindtrade.com/api/v4/data"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HISTORY_START = {"price": "2015-01-01", "inst": "2018-01-01", "revenue": "2015-01-01"}
SLEEP_SECONDS = 1.0  # 免費帳號每小時有次數上限，放慢一點比較安全

PRICE_COLS = ["date", "open", "high", "low", "close", "volume"]
INST_COLS = ["date", "foreign_net", "trust_net", "dealer_net"]
REV_COLS = ["date", "revenue_year", "revenue_month", "revenue"]


class QuotaExceeded(Exception):
    """FinMind 回報次數用完；停止這次執行，已抓到的先存起來，下次再接著抓。"""


def fetch(dataset, data_id, start_date, token, opener=None):
    query = urllib.parse.urlencode({"dataset": dataset, "data_id": data_id, "start_date": start_date})
    req = urllib.request.Request(f"{API}?{query}", headers={"Authorization": f"Bearer {token}"})
    opener = opener or urllib.request.urlopen
    for attempt in range(3):
        try:
            with opener(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 402:
                raise QuotaExceeded(f"{dataset} {data_id}: HTTP 402")
            if attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(5 * (attempt + 1))
    status = body.get("status")
    if status == 402:
        raise QuotaExceeded(f"{dataset} {data_id}: {body.get('msg')}")
    if status not in (200, None):
        raise RuntimeError(f"{dataset} {data_id}: status {status} {body.get('msg')}")
    return body.get("data") or []


def read_rows(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_rows(path, cols, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def merge(existing, new_rows):
    """以 date 為鍵合併，新資料覆蓋舊資料，依日期排序。"""
    by_date = {r["date"]: r for r in existing}
    for r in new_rows:
        by_date[r["date"]] = r
    return [by_date[d] for d in sorted(by_date)]


def next_start(rows, kind):
    if not rows:
        return HISTORY_START[kind]
    last = date.fromisoformat(rows[-1]["date"])
    # 往回多抓幾天，萬一前一次抓到的是盤中或尚未修正的資料，可以覆蓋
    return (last - timedelta(days=5)).isoformat()


def num(x):
    if x in (None, ""):
        return ""
    v = float(x)
    return int(v) if v.is_integer() else round(v, 4)


def parse_price(data):
    out = []
    for d in data:
        if not d.get("close"):
            continue
        out.append({
            "date": d["date"],
            "open": num(d.get("open")),
            "high": num(d.get("max")),
            "low": num(d.get("min")),
            "close": num(d.get("close")),
            "volume": num(d.get("Trading_Volume")),
        })
    return out


FOREIGN = {"Foreign_Investor", "Foreign_Dealer_Self"}
TRUST = {"Investment_Trust"}
DEALER = {"Dealer_self", "Dealer_Hedging"}


def parse_inst(data):
    agg = {}
    for d in data:
        row = agg.setdefault(d["date"], {"date": d["date"], "foreign_net": 0, "trust_net": 0, "dealer_net": 0})
        net = int(float(d.get("buy") or 0) - float(d.get("sell") or 0))
        name = d.get("name")
        if name in FOREIGN:
            row["foreign_net"] += net
        elif name in TRUST:
            row["trust_net"] += net
        elif name in DEALER:
            row["dealer_net"] += net
    return [agg[k] for k in sorted(agg)]


def parse_revenue(data):
    return [{
        "date": d["date"],
        "revenue_year": d.get("revenue_year", ""),
        "revenue_month": d.get("revenue_month", ""),
        "revenue": num(d.get("revenue")),
    } for d in data]


def is_etf_or_index(code):
    return code == "TAIEX" or code.startswith("00")


def load_tickers():
    codes = []
    for line in (ROOT / "tickers.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line not in codes:
            codes.append(line)
    return codes


def update(kind, dataset, code, parser, cols, token, opener, latest_trading_date=None):
    path = DATA / {"price": "prices", "inst": "inst", "revenue": "revenue"}[kind] / f"{code}.csv"
    rows = read_rows(path)
    if latest_trading_date and rows and rows[-1]["date"] >= latest_trading_date:
        return rows, False  # 已經是最新，不浪費查詢次數
    new = parser(fetch(dataset, code, next_start(rows, kind), token, opener))
    time.sleep(SLEEP_SECONDS)
    if not new:
        return rows, False
    rows = merge(rows, new)
    write_rows(path, cols, rows)
    return rows, True


def revenue_is_stale(code):
    rows = read_rows(DATA / "revenue" / f"{code}.csv")
    if not rows:
        return True
    # 月營收每月 10 日前公布；最後一筆超過 35 天就再查一次
    return (date.today() - date.fromisoformat(rows[-1]["date"])).days > 35


def build_latest(codes):
    out = []
    for code in codes:
        prices = read_rows(DATA / "prices" / f"{code}.csv")
        if not prices:
            continue
        last = prices[-1]
        prev = prices[-2] if len(prices) > 1 else None
        row = {"code": code, "date": last["date"], "close": last["close"], "volume": last["volume"],
               "prev_date": prev["date"] if prev else "", "prev_close": prev["close"] if prev else "",
               "pct_change": ""}
        if prev and prev["close"]:
            row["pct_change"] = round((float(last["close"]) / float(prev["close"]) - 1) * 100, 2)
        inst = {r["date"]: r for r in read_rows(DATA / "inst" / f"{code}.csv")}.get(last["date"])
        for k in ("foreign_net", "trust_net", "dealer_net"):
            row[k] = inst[k] if inst else ""
        out.append(row)
    write_rows(DATA / "latest.csv", ["code", "date", "close", "pct_change", "prev_date", "prev_close", "volume",
                                     "foreign_net", "trust_net", "dealer_net"], out)
    return out


def main(opener=None):
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if not token:
        sys.exit("找不到 FINMIND_TOKEN：請到 GitHub 專案的 Settings → Secrets and variables → Actions 新增。")
    codes = load_tickers()
    failures, updated = [], 0
    latest_trading_date = None

    try:
        # 先抓 0050，用它的最後日期當「最新交易日」，其他已是最新的代號就跳過
        bench, _ = update("price", "TaiwanStockPrice", "0050", parse_price, PRICE_COLS, token, opener)
        latest_trading_date = bench[-1]["date"] if bench else None
        for code in codes:
            try:
                _, changed = update("price", "TaiwanStockPrice", code, parse_price, PRICE_COLS, token, opener,
                                    latest_trading_date)
                updated += changed
                if not is_etf_or_index(code):
                    update("inst", "TaiwanStockInstitutionalInvestorsBuySell", code, parse_inst, INST_COLS,
                           token, opener, latest_trading_date)
                    if revenue_is_stale(code):
                        update("revenue", "TaiwanStockMonthRevenue", code, parse_revenue, REV_COLS, token, opener)
            except QuotaExceeded:
                raise
            except Exception as e:  # 單一代號失敗不影響其他代號
                failures.append({"code": code, "error": str(e)[:200]})
                print(f"[失敗] {code}: {e}", file=sys.stderr)
    except QuotaExceeded as e:
        failures.append({"code": "*", "error": f"查詢次數用完，下次執行會接著抓：{e}"})
        print(f"[次數用完] {e}", file=sys.stderr)

    latest = build_latest(codes)
    meta = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latest_trading_date": latest_trading_date,
        "tickers": len(codes),
        "tickers_with_prices": len(latest),
        "price_files_updated": updated,
        "failures": failures,
    }
    (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
