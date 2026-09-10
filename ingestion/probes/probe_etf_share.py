"""P0 probe: ETF share history & NAV (design sec.3: 历史而非只有当日).

Confirmed channels (P0 2026-09-10):
- listing universe: EM clist via push2delay (direct client)
- exchange bars: sina fund_etf_hist_sina (FULL history 2012+, incl volume)
- official NAV: EM fundapi fund_open_fund_info_em (verified in fund_holdings)
- quarterly share change: fundf10 gmbd HTML (quarterly only — gap for daily ΔS)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import Probe, df_artifact  # noqa: E402

import akshare as ak  # noqa: E402
import em_client  # noqa: E402

P = Probe("etf_share")

# ---- 1. ETF universe via EM clist (push2delay) ---------------------------
def _universe():
    df = em_client.etf_list()
    return df_artifact(df, "etf_share", "etf_universe_em",
                       "em_client:etf_list (upstream: eastmoney clist)", "eastmoney")
P.check("etf_universe_list", "eastmoney", "em_client.etf_list", _universe,
        expect_min_rows=500, note="code/name/mcap columns; f20 total/f21 float mcap")

# ---- 2. exchange-listed full bars via sina -------------------------------
for sym in ("sh510300", "sz159915"):
    def _f(sym=sym):
        df = ak.fund_etf_hist_sina(symbol=sym)
        return df_artifact(df, "etf_share", f"etf_bars_sina_{sym}",
                           f"akshare:fund_etf_hist_sina/{sym} (upstream: sina)", "sina")
    P.check(f"etf_bars_history_{sym}", "sina", "akshare.fund_etf_hist_sina", _f,
            expect_min_rows=2000, note="full exchange history incl volume/amount")

# ---- 3. official NAV (fundapi) --------------------------------------------
def _nav():
    df = ak.fund_open_fund_info_em(symbol="510300", indicator="单位净值走势")
    return df_artifact(df, "etf_share", "etf_nav_510300",
                       "akshare:fund_open_fund_info_em/510300 (upstream: eastmoney fundapi)", "eastmoney")
P.check("etf_nav_history_510300", "eastmoney", "akshare.fund_open_fund_info_em", _nav,
        expect_min_rows=400)

# ---- 4. QUARTERLY share change from fundf10 (KEY granularity check) ------
for code in ("510300", "159915"):
    def _f(code=code):
        df = em_client.fund10_share_change(code)
        return df_artifact(df, "etf_share", f"etf_share_change_q_{code}",
                           f"em_client:fund10_share_change/{code} (upstream: eastmoney fundf10)", "eastmoney")
    P.check(f"etf_share_change_quarterly_{code}", "eastmoney", "em_client.fund10_share_change", _f,
            expect_min_rows=8, note="quarterly 申购/赎回/份额; daily ΔS needs another path")

# ---- 5. EM daily ETF table (netvalue+share per day? akshare entry) --------
def _daily_em():
    df = ak.fund_etf_fund_daily_em()
    return df_artifact(df, "etf_share", "etf_fund_daily_em",
                       "akshare:fund_etf_fund_daily_em (upstream: eastmoney fund)", "eastmoney")
P.check("etf_daily_table_em", "eastmoney", "akshare.fund_etf_fund_daily_em", _daily_em,
        expect_min_rows=200, note="single-day snapshot vs history — check columns")

P.finish({
    "goal": "historical per-ETF share series for net subscription est. F≈ΔS×NAV_t",
    "risk": "share change is QUARTERLY at fundf10; daily ΔS path = exchange bar volume proxy "
            "(creation unit estimation) or datacenter-web daily share field (blocked/intermittent)",
})
