"""P0 probe: ETF share history & NAV (KEY risk item per design sec.3:
"ETF份额重点核查历史而非只有当日").

Discovers which akshare entries actually expose HISTORICAL fund share (份额)
series, not just today's snapshot. Net subscription/redemption F≈ΔS×NAV_t
(design sec.7.1) requires daily share series per ETF.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import Probe, df_artifact  # noqa: E402

import akshare as ak  # noqa: E402

P = Probe("etf_share")

# ---- 0. discovery: what fund_etf* / share-related APIs exist -----------
names = [n for n in dir(ak) if ("etf" in n.lower()) or ("share" in n.lower() and "fund" in n.lower())]
print("[etf_share] candidate apis:", names, flush=True)
P.check("api_discovery", "meta", "dir(ak)", lambda: names, expect_min_rows=10,
        note="catalog of etf-related adapter entries in this akshare build")

# ---- 1. all-ETF daily table (net value / share / scale per day?) -------
def _daily():
    df = ak.fund_etf_fund_daily_em()
    return df_artifact(df, "etf_share", "etf_fund_daily_em",
                       "akshare:fund_etf_fund_daily_em (upstream: eastmoney)", "eastmoney")
P.check("etf_daily_table", "eastmoney", "akshare.fund_etf_fund_daily_em", _daily,
        expect_min_rows=500, note="check columns for 份额/规模 history vs single-day")

# ---- 2. spot snapshot (today only; contrast item) ----------------------
def _spot():
    df = ak.fund_etf_spot_em()
    return df_artifact(df, "etf_share", "etf_spot_em",
                       "akshare:fund_etf_spot_em (upstream: eastmoney)", "eastmoney")
P.check("etf_spot_today", "eastmoney", "akshare.fund_etf_spot_em", _spot,
        expect_min_rows=500, note="today-only snapshot; NOT sufficient for ΔS history")

# ---- 3. per-ETF listed market history (price/volume, not shares) -------
def _hist510300():
    df = ak.fund_etf_hist_em(symbol="510300", period="daily",
                             start_date="20230801", end_date="20260910", adjust="qfq")
    return df_artifact(df, "etf_share", "etf_hist_510300",
                       "akshare:fund_etf_hist_em/510300 (upstream: eastmoney)", "eastmoney")
P.check("etf_market_history_510300", "eastmoney", "akshare.fund_etf_hist_em", _hist510300,
        expect_min_rows=400, note="exchange-listed bars; volume allows share proxy only via 申赎清单")

# ---- 4. per-ETF F10 scale-change history (份额变动) ---------------------
def _scale_change():
    fn = None
    for cand in ("fund_etf_scale_change_em", "fund_scale_change_em", "fund_etf_fund_info_em"):
        fn = getattr(ak, cand, None)
        if fn is not None:
            break
    if fn is None:
        raise KeyError("no fund scale-change api; check discovery list")
    try:
        df = fn(symbol="510300")
    except TypeError:
        df = fn(symbol="510300", indicator="基金份额变动")
    return df_artifact(df, "etf_share", f"etf_scale_change_510300",
                       f"akshare:{fn.__name__} (upstream: eastmoney)", "eastmoney")
P.check("etf_share_change_history_510300", "eastmoney",
        "akshare.fund_etf_scale_change_em|similar", _scale_change,
        note="CRITICAL: does free path expose quarterly/daily share history? quarterly=fallback")

# ---- 5. open-fund info per ETF (NAV series) ----------------------------
def _nav():
    df = ak.fund_open_fund_info_em(symbol="510300", indicator="单位净值走势")
    return df_artifact(df, "etf_share", "etf_nav_510300",
                       "akshare:fund_open_fund_info_em/510300 (upstream: eastmoney)", "eastmoney")
P.check("etf_nav_history_510300", "eastmoney", "akshare.fund_open_fund_info_em", _nav,
        expect_min_rows=400, note="official NAV series for ΔS×NAV_t")

# ---- 6. exchange official monthly ETF scale (SSE) -----------------------
def _sse_monthly():
    fn = getattr(ak, "fund_etf_category_sina", None)
    if fn is None:
        raise KeyError("no sina etf category api")
    df = fn(symbol="ETF基金")
    return df_artifact(df, "etf_share", "etf_category_sina",
                       f"akshare:{fn.__name__} (upstream: sina)", "sina")
P.check("etf_list_sina", "sina", "akshare.fund_etf_category_sina", _sse_monthly,
        expect_min_rows=100, note="sina ETF list as cross-check universe")

P.finish({
    "goal": "historical per-ETF share series for net subscription est. F≈ΔS×NAV",
    "risk": "if only quarterly share data exists, 1/5/20/60d net flows degrade to interpolation or exchange-listed volume proxy",
})
