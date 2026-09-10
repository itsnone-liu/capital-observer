"""Observation layer (P2): pure fact-derived views, NO estimation here.

Design boundary: this module computes descriptive series straight from
fact_observation (published data): index levels, sector ranking, margin
trends, ETF size decomposition. Estimates (L1-L3) live in the scenario
engine (P3) and are always labeled — these views are facts + arithmetic.

Every view returns: data, as_of, coverage, denominator, warnings.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from storage.models import Asset, FactObservation

DB_URL = None  # set by init


def _facts_df(session: Session, subject_key: str, metric: str,
              start: str | None = None) -> pd.DataFrame:
    q = (select(FactObservation.effective_at, FactObservation.value,
                FactObservation.quality_status)
         .where(FactObservation.subject_key == subject_key,
                FactObservation.metric == metric,
                FactObservation.quality_status.in_(("valid", "degraded")))
         .order_by(FactObservation.effective_at))
    rows = session.execute(q).all()
    df = pd.DataFrame(rows, columns=["date", "value", "q"])
    if df.empty:
        return df
    if start:
        df = df[df["date"] >= start]
    # latest-revision-wins per date (append-only chain: max id per date kept)
    df = df.groupby("date", as_index=False).agg(value=("value", "last"), q=("q", "last"))
    df["value"] = pd.to_numeric(df["value"])
    return df


def _ret(series: pd.Series, n: int) -> float | None:
    if len(series) <= n:
        return None
    return float(series.iloc[-1] / series.iloc[-1 - n] - 1.0)


def market_overview(session: Session, indices: list[str], window: int = 60) -> dict:
    """Index levels, returns, turnover; as_of = latest common trade date."""
    out = {}
    as_of = None
    for sym in indices:
        df = _facts_df(session, f"index:{sym}", "index_close")
        if df.empty:
            out[sym] = {"status": "no_data"}
            continue
        last = df.iloc[-1]
        as_of = max(as_of or "", str(last["date"]))
        amt = _facts_df(session, f"index:{sym}", "index_amount")
        out[sym] = {
            "close": float(last["value"]), "date": str(last["date"]),
            "ret_1d": _ret(df["value"], 1), "ret_5d": _ret(df["value"], 5),
            "ret_20d": _ret(df["value"], 20), "ret_60d": _ret(df["value"], 60),
            "obs_days": int(len(df)),
            "amount_20d_avg_yuan": (float(amt["value"].tail(20).mean())
                                    if not amt.empty and len(amt) >= 5 else None),
        }
    return {"data": out, "as_of": as_of, "coverage": f"{sum(1 for v in out.values() if v.get('close'))}/{len(indices)}",
            "denominator": "指数公开收盘/成交额（新浪，全历史）", "warnings": []}


def sector_ranking(session: Session, lookbacks: tuple[int, ...] = (5, 20, 60),
                   as_of: str | None = None, amount_metric: str = "sw_amount") -> dict:
    """SW L1 industry ranking by return over windows + turnover share.

    Denominators explicit: return uses each industry's own close series;
    turnover share uses sum over industries WITH data on that date (coverage noted).
    """
    names = dict(session.execute(
        select(Asset.asset_key, Asset.name).where(Asset.asset_type == "industry")).all())
    rows = session.execute(
        select(FactObservation.subject_key).distinct()
        .where(FactObservation.metric == "sw_close")).scalars().all()
    data = {}
    amt_totals: dict[str, dict[str, float]] = {}
    for subj in rows:
        ind = subj.split(":")[-1]
        df = _facts_df(session, subj, "sw_close")
        if df.empty:
            continue
        if as_of:
            df = df[df["date"] <= as_of]
        rec = {"close": float(df["value"].iloc[-1]), "date": str(df["date"].iloc[-1]),
               "obs_days": int(len(df))}
        for n in lookbacks:
            rec[f"ret_{n}d"] = _ret(df["value"], n)
        data[ind] = rec
        amt = _facts_df(session, subj, amount_metric)
        if not amt.empty:
            if as_of:
                amt = amt[amt["date"] <= as_of]
            tail = amt.tail(20)
            # SW amounts are published in 亿元 (yuan_100m) — keep native unit
            data[ind]["amount_20d_avg_yi"] = float(tail["value"].mean())
            for _, r in amt.tail(5).iterrows():
                amt_totals.setdefault(str(r["date"]), {})[ind] = float(r["value"])
    # turnover share on latest common date
    if amt_totals:
        latest_day = max(amt_totals)
        tot = sum(amt_totals[latest_day].values())
        for ind, v in amt_totals[latest_day].items():
            if ind in data and tot > 0:
                data[ind]["turnover_share_latest"] = v / tot
        denom_note = f"成交额份额分母={latest_day}有数据的{len(amt_totals[latest_day])}个行业"
    else:
        denom_note = "无成交额数据"
    ranked = sorted(data.items(), key=lambda kv: kv[1].get("ret_20d") or -9, reverse=True)
    return {"data": [{"industry": k, "name": names.get(k, ""), **v} for k, v in ranked],
            "as_of": max((v["date"] for v in data.values()), default=None),
            "coverage": f"{len(data)}/31 申万一级行业",
            "denominator": f"各行业自身收盘序列；{denom_note}", "warnings": []}


def margin_view(session: Session, windows: tuple[int, ...] = (1, 5, 20, 60)) -> dict:
    """Market margin balance/buy series + changes (SSE exchange-published).

    SSE only in v1 (SZSE summary is per-day loop — merged when collected).
    No ratio computed without mcap denominator (explicitly None then).
    """
    bal = _facts_df(session, "market:SSE", "margin_fin_balance")
    buy = _facts_df(session, "market:SSE", "margin_fin_buy")
    if bal.empty:
        return {"data": None, "as_of": None, "coverage": "0", "denominator": "",
                "warnings": ["上交所两融汇总无数据"]}
    d = {"latest_balance_yuan": float(bal["value"].iloc[-1]),
         "as_of": str(bal["date"].iloc[-1])}
    for n in windows:
        if len(bal) > n:
            d[f"balance_chg_{n}d_yuan"] = float(bal["value"].iloc[-1] - bal["value"].iloc[-1 - n])
        if not buy.empty and len(buy) > n:
            d[f"buy_sum_{n}d_yuan"] = float(buy["value"].tail(n).sum())
    d["obs_days"] = int(len(bal))
    return {"data": d, "as_of": d["as_of"], "coverage": "SSE only (SZSE待并)",
            "denominator": "上交所披露余额原值（元）；变化=窗口首尾差",
            "warnings": ["SZSE 汇总为逐日接口，v1 仅上交所口径，勿当作全市场"]}


def etf_size_decomposition(session: Session, code: str) -> dict:
    """ΔA = ΔS×NAV_t + S_{t-1}×ΔNAV (design sec.7.1) at QUARTERLY granularity.

    Honest version: share data is quarterly (fundf10), NAV daily -> the
    decomposition lands on quarter ends; residual noted. No daily ΔS fabrication.
    """
    nav = _facts_df(session, f"etf:{code}", "etf_nav")
    share = _facts_df(session, f"etf:{code}", "etf_share_end")
    if nav.empty or share.empty:
        return {"data": None, "as_of": None, "coverage": f"etf:{code} nav={len(nav)} share={len(share)}",
                "denominator": "", "warnings": ["数据不足"]}
    share = share.sort_values("date")
    rows = []
    nav_idx = nav.set_index("date")["value"]
    share = share[share["date"].str.len() == 10]  # drop any junk dates
    for i in range(1, len(share)):
        q0, q1 = share.iloc[i - 1], share.iloc[i]
        nav_at = [nav_idx.get(d) for d in (q0["date"], q1["date"])]
        if not (nav_at[0] and nav_at[1]):
            continue
        ds = float(q1["value"] - q0["value"])
        a0 = float(q0["value"]) * nav_at[0]
        a1 = float(q1["value"]) * nav_at[1]
        flow = ds * nav_at[1]              # share change valued at END-of-window NAV (stated basis)
        val = float(q0["value"]) * (nav_at[1] - nav_at[0])
        rows.append({"period": f"{q0['date']}->{q1['date']}",
                     "nav_basis": "期末净值计价", "size_start_yuan": a0, "size_end_yuan": a1,
                     "flow_component_yuan": flow, "valuation_component_yuan": val,
                     "residual_yuan": (a1 - a0) - flow - val})
    rows = rows[::-1]  # latest period first
    return {"data": rows, "as_of": str(share["date"].iloc[-1]),
            "coverage": f"季度份额{len(share)}期×日度净值", 
            "denominator": "份额=基金披露期末份额(万份×1e4)；规模=份额×单位净值(未含费用调整)",
            "warnings": ["季度粒度：期间申赎时点未知，flow计价基差在区间内不分摊（设计文档L1宽口径）"]}
