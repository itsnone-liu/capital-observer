"""P0 probe: margin trading (两融) — balance/buy/repay, market level and
industry aggregation path (design sec.7.2: industry concentration, ratios
to free-float mcap & turnover; NOT summed with ETF flows).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import Probe, df_artifact, rate_limit  # noqa: E402

import akshare as ak  # noqa: E402

P = Probe("margin")

names = [n for n in dir(ak) if "margin" in n.lower()]
print("[margin] candidate apis:", names, flush=True)
P.check("api_discovery", "meta", "dir(ak)", lambda: names, expect_min_rows=5,
        note="margin-related adapter entries")

# ---- 1. SSE market-level margin summary history ------------------------
def _sse():
    df = ak.stock_margin_sse(start_date="20230801", end_date="20260910")
    return df_artifact(df, "margin", "margin_sse",
                       "akshare:stock_margin_sse (upstream: sse.com.cn)", "sse")
P.check("sse_margin_summary", "sse", "akshare.stock_margin_sse", _sse,
        expect_min_rows=400, note="market-level 融资买入/偿还/余额 daily")

# ---- 2. SZSE margin summary (per-day API; loop a small window) ----------
def _szse():
    frames = []
    for d in ("20260907", "20260908", "20260909"):
        rate_limit("szse", 1.5)
        frames.append(ak.stock_margin_szse(date=d))
    import pandas as pd
    df = pd.concat(frames, ignore_index=True)
    return df_artifact(df, "margin", "margin_szse_3d",
                       "akshare:stock_margin_szse (upstream: szse.cn)", "szse")
P.check("szse_margin_summary", "szse", "akshare.stock_margin_szse", _szse,
        expect_min_rows=2, note="per-day API (no range args); daily loop needed for history")

# ---- 3. per-underlying detail (needed for industry aggregation) --------
def _sse_detail():
    df = ak.stock_margin_detail_sse(date="20260909")
    return df_artifact(df, "margin", "margin_detail_sse_20260909",
                       "akshare:stock_margin_detail_sse (upstream: sse.com.cn)", "sse")
P.check("sse_margin_underlying_detail", "sse", "akshare.stock_margin_detail_sse", _sse_detail,
        expect_min_rows=500, note="per-stock margin; join with industry map for sector concentration")

def _szse_detail():
    df = ak.stock_margin_detail_szse(date="20260909")
    return df_artifact(df, "margin", "margin_detail_szse_20260909",
                       "akshare:stock_margin_detail_szse (upstream: szse.cn)", "szse")
P.check("szse_margin_underlying_detail", "szse", "akshare.stock_margin_detail_szse", _szse_detail,
        expect_min_rows=500, note="per-stock margin detail")

# ---- 4. EM datacenter margin (different EM family: datacenter-web) --------
def _em_detail():
    # datacenter-web hosts margin aggregates; test the daily margin report API
    import requests
    UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "reportName": "RPTA_WEB_RZRQ_GGMX", "columns": "ALL", "pageSize": "20",
        "pageNumber": "1", "sortColumns": "dim_date", "sortTypes": "-1",
        "filter": '(scode="000001")',
    }
    r = requests.get(url, params=params, headers=UA, timeout=15)
    r.raise_for_status()
    js = r.json()
    rows = ((js.get("result") or {}).get("data")) or []
    import pandas as pd
    df = pd.DataFrame(rows)
    return df_artifact(df, "margin", "margin_detail_datacenter_000001",
                       f"datacenter-web:RPTA_WEB_RZRQ_GGMX (upstream: eastmoney)", "eastmoney")
P.check("em_datacenter_margin_detail", "eastmoney", "datacenter-web RPTA_WEB_RZRQ_GGMX", _em_detail,
        note="datacenter-web family (distinct from blocked push2); per-stock margin history")

# ---- 5. how far back do exchange summaries go? --------------------------
def _sse_long():
    df = ak.stock_margin_sse(start_date="20200101", end_date="20230801")
    return df_artifact(df, "margin", "margin_sse_2020_2023",
                       "akshare:stock_margin_sse (upstream: sse.com.cn)", "sse")
P.check("sse_margin_history_depth", "sse", "akshare.stock_margin_sse", _sse_long,
        expect_min_rows=500, note="2020-2023 window to test multi-year availability")

P.finish({
    "goal": "market+industry margin series; per-underlying detail enables industry concentration",
    "risk": "detail tables are per-day single files -> backfill loop cost; watch per-day rate limits",
})
