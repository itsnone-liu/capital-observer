"""P0 probe: fund position disclosures (季报重仓 vs 完整持仓 — design sec.3
explicitly separates them; sec.7.3 comparison rules; sec.7.4 nowcast prior).

Target sample: 3-5 equity funds across 2 types per first-batch plan
(50-100 funds eventually).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import Probe, df_artifact  # noqa: E402

import akshare as ak  # noqa: E402

P = Probe("fund_holdings")

names = [n for n in dir(ak) if "portfolio" in n.lower() or "fund_report" in n.lower()]
print("[fund_holdings] candidate apis:", names, flush=True)
P.check("api_discovery", "meta", "dir(ak)", lambda: names, expect_min_rows=3,
        note="fund portfolio/report adapter entries")

SAMPLE_FUNDS = [
    ("000001", "华夏成长混合", "混合"),
    ("110022", "易方达消费行业", "股票"),
    ("005827", "易方达蓝筹精选", "混合"),
]

# ---- 1. quarterly top holdings (季报重仓 top10) --------------------------
for code, label, ftype in SAMPLE_FUNDS[:2]:
    def _f(code=code):
        df = ak.fund_portfolio_hold_em(symbol=code, date="20251")
        return df_artifact(df, "fund_holdings", f"hold_{code}_2025Q1",
                           f"akshare:fund_portfolio_hold_em/{code} (upstream: eastmoney/tiantian)", "eastmoney")
    P.check(f"quarterly_top_holdings_{code}", "eastmoney", "akshare.fund_portfolio_hold_em", _f,
            expect_min_rows=5, note=f"{label} {ftype}; expect ~10 top holdings, scope=top_holdings")

# ---- 2. full portfolio availability check (年报/半年报全持仓) ------------
def _full():
    df = ak.fund_portfolio_hold_em(symbol="005827", date="20241")
    n = df.shape[0]
    print(f"[fund_holdings] 005827 2024 annual rows={n} (full portfolio would be >>10)", flush=True)
    return df_artifact(df, "fund_holdings", "hold_005827_2024_annual",
                       "akshare:fund_portfolio_hold_em/005827 (upstream: eastmoney)", "eastmoney")
P.check("annual_full_portfolio_005827", "eastmoney", "akshare.fund_portfolio_hold_em", _full,
        note="annual report may expose FULL holdings; rows>30 suggests full; still labeled disclosure_scope")

# ---- 3. fund industry allocation (行业配置) ------------------------------
def _industry():
    df = ak.fund_portfolio_industry_allocation_em(symbol="005827", date="2025")
    return df_artifact(df, "fund_holdings", "industry_alloc_005827_2025",
                       "akshare:fund_portfolio_industry_allocation_em (upstream: eastmoney)", "eastmoney")
P.check("fund_industry_allocation", "eastmoney", "akshare.fund_portfolio_industry_allocation_em", _industry,
        note="reported industry weights; useful prior for nowcast")

# ---- 4. fund NAV + share scale series (nowcast input) --------------------
def _nav():
    df = ak.fund_open_fund_info_em(symbol="005827", indicator="单位净值走势")
    return df_artifact(df, "fund_holdings", "nav_005827",
                       "akshare:fund_open_fund_info_em/005827 (upstream: eastmoney)", "eastmoney")
P.check("fund_nav_history", "eastmoney", "akshare.fund_open_fund_info_em", _nav,
        expect_min_rows=500)

# ---- 5. fund scale (资产净值/份额 quarterly from 分红送配详情页 f10) -------
def _scale():
    fn = None
    for cand in ("fund_scale_change_em", "fund_etf_scale_change_em"):
        fn = getattr(ak, cand, None)
        if fn:
            break
    if fn is None:
        raise KeyError("no fund_scale_change_em")
    df = fn(symbol="005827")
    return df_artifact(df, "fund_holdings", "scale_change_005827",
                       f"akshare:{fn.__name__} (upstream: eastmoney)", "eastmoney")
P.check("fund_share_scale_change", "eastmoney", "akshare.fund_scale_change_em", _scale,
        note="quarterly share/size series; A/C share-class merge needed downstream")

# ---- 6. fund universe list (to sample 50-100 equity funds) ----------------
def _universe():
    df = ak.fund_open_fund_rank_em(symbol="全部")
    eq = df[df["基金类型"].isin(["股票型", "混合型", "股票指数", "联接基金"])] if "基金类型" in df.columns else df
    print(f"[fund_holdings] universe rows={df.shape[0]} equity-ish={eq.shape[0]}", flush=True)
    return df_artifact(df, "fund_holdings", "open_fund_rank_universe",
                       "akshare:fund_open_fund_rank_em (upstream: eastmoney)", "eastmoney")
P.check("open_fund_universe", "eastmoney", "akshare.fund_open_fund_rank_em", _universe,
        expect_min_rows=5000, note="sampling frame for 50-100 active equity funds")

P.finish({
    "goal": "quarterly top-10 holdings + annual full portfolio + industry alloc + NAV/scale",
    "risk": "holdings disclosure lag (15工作日/季度, 60日/年报) — nowcast is weekly but priors are quarterly",
})
