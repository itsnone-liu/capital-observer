"""FastAPI app (design sec.11): facts-first endpoints, every response carries
data/as_of/coverage/denominator(run_id comes with P3 estimates)/warnings.

Run: python3 -m uvicorn api.app:app --port 8120
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from models.observations import (etf_size_decomposition, margin_view,  # noqa: E402
                                 market_overview, sector_ranking)
from storage.models import FactObservation, JobRun  # noqa: E402

DB_URL = f"sqlite:///{ROOT / 'data' / 'capobs.db'}"
engine = create_engine(DB_URL, future=True, connect_args={"check_same_thread": False})

app = FastAPI(title="capital-observer", version="0.1.0-p2")

from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/dashboard", StaticFiles(directory=str(ROOT / "dashboard"), html=True), name="dashboard")

INDICES = ["sh000300", "sh000016", "sh000905", "sh000852", "sh000912",
           "sz399006", "sh000688"]


def _session():
    return Session(engine, future=True)


@app.get("/api/v1/market/overview")
def get_overview(as_of: str | None = Query(None, description="YYYY-MM-DD replay")):
    with _session() as s:
        v = market_overview(s, INDICES)
    if as_of:
        v["warnings"].append(f"as_of={as_of} 重放未在 v1 实现，返回最新")
    v["published_cutoff"] = v["as_of"]
    return v


@app.get("/api/v1/sectors/ranking")
def get_sectors(sort: str = Query("ret_20d", pattern="^ret_(5|20|60)d$"),
                as_of: str | None = None):
    with _session() as s:
        v = sector_ranking(s, as_of=as_of)
    if sort != "ret_20d":
        v["data"] = sorted(v["data"], key=lambda d: d.get(sort) or -9, reverse=True)
    v["published_cutoff"] = v["as_of"]
    return v


@app.get("/api/v1/margin/summary")
def get_margin():
    with _session() as s:
        v = margin_view(s)
    v["published_cutoff"] = v["as_of"]
    return v


ETF_FIRST_NAMES = {"510300": "沪深300ETF", "510050": "上证50ETF", "510500": "中证500ETF",
                   "512100": "中证1000ETF", "159915": "创业板ETF", "588000": "科创50ETF",
                   "563300": "中证2000ETF", "512480": "半导体ETF", "512760": "芯片ETF",
                   "512690": "酒ETF", "515790": "光伏ETF", "512800": "银行ETF",
                   "512010": "医药ETF", "159928": "消费ETF", "515030": "新能源车ETF"}


@app.get("/api/v1/etf/flows")
def get_etf_flows(periods: int = Query(4, ge=1, le=12)):
    """Quarterly net subscribe/redeem across first-batch ETFs (quarter facts)."""
    rows = engine.connect().execute(
        select(FactObservation.subject_key, FactObservation.effective_at,
               FactObservation.metric, FactObservation.value)
        .where(FactObservation.metric.in_(
            ("etf_share_subscribe", "etf_share_redeem", "etf_share_end", "etf_net_asset")),
            FactObservation.quality_status == "valid")
        .order_by(FactObservation.effective_at.desc())).all()
    by_etf: dict[str, dict] = {}
    for subj, date, metric, value in rows:
        code = subj.split(":")[-1]
        e = by_etf.setdefault(code, {"name": ETF_FIRST_NAMES.get(code, code), "quarters": {}})
        q = e["quarters"].setdefault(date, {})
        q[metric] = float(value)
    data = []
    for code, e in by_etf.items():
        qs = sorted(e["quarters"].items(), reverse=True)[:periods]
        qlist = []
        for d, q in qs:
            sub, red = q.get("etf_share_subscribe"), q.get("etf_share_redeem")
            qlist.append({"period": d, "subscribe": sub, "redeem": red,
                          "net": (sub - red) if (sub is not None and red is not None) else None,
                          "share_end": q.get("etf_share_end")})
        data.append({"code": code, "name": e["name"], "quarters": qlist})
    data.sort(key=lambda x: -(x["quarters"][0]["net"] or 0) if x["quarters"] and x["quarters"][0]["net"] is not None else 0)
    latest_periods = sorted({q["period"] for e in data for q in e["quarters"]}, reverse=True)
    return {"data": data, "as_of": latest_periods[0] if latest_periods else None,
            "coverage": f"{len(data)}只首批ETF（季度披露）",
            "denominator": "基金披露期间申购/赎回份额（亿份×1e8）；季度粒度，期间时点未知",
            "warnings": ["净申赎=申购-赎回（份额口径）；与价格口径的成交额不可比",],
            "published_cutoff": latest_periods[0] if latest_periods else None}


@app.get("/api/v1/cost/fund/{fund}")
def get_fund_cost(fund: str, impact: str = Query("base", pattern="^(low|base|high)$"),
                  aum_yi: float = Query(100.0, gt=0, le=20000)):
    """Rebalance cost estimate between last two disclosed industry weights."""
    from models.cost import fund_rebalance_cost
    with _session() as s:
        rows = s.execute(
            select(FactObservation.asset_key, FactObservation.value,
                   FactObservation.effective_at)
            .where(FactObservation.subject_key == f"fund:{fund}",
                   FactObservation.metric == "fund_industry_weight",
                   FactObservation.quality_status == "valid")
            .order_by(FactObservation.effective_at.desc())).all()
        by_q: dict[str, dict[str, float]] = {}
        for a, v, q in rows:
            by_q.setdefault(str(q), {})[a.split(":", 1)[-1]] = float(v)
        qs = sorted(by_q, reverse=True)
        if len(qs) < 2:
            raise HTTPException(404, f"need >=2 disclosure periods for {fund}, have {len(qs)}")
        v = fund_rebalance_cost(s, fund, by_q[qs[1]], by_q[qs[0]],
                                aum_yi * 1e8, impact_scenario=impact)
    v["as_of"] = qs[0]
    return v


@app.get("/api/v1/etf/{code}/structure")
def get_etf_structure(code: str):
    if not (len(code) == 6 and code.isdigit()):
        raise HTTPException(400, "code must be 6-digit fund code")
    with _session() as s:
        v = etf_size_decomposition(s, code)
    v["published_cutoff"] = v["as_of"]
    return v


@app.get("/api/v1/margin/history")
def get_margin_history(days: int = Query(120, ge=10, le=2000)):
    import pandas as pd
    with _session() as s:
        rows = s.execute(
            select(FactObservation.effective_at, FactObservation.metric,
                   FactObservation.value)
            .where(FactObservation.subject_key == "market:SSE",
                   FactObservation.metric.in_(("margin_fin_balance", "margin_fin_buy")),
                   FactObservation.quality_status == "valid")
            .order_by(FactObservation.effective_at)).all()
    if not rows:
        raise HTTPException(503, "no margin data")
    df = pd.DataFrame(rows, columns=["date", "metric", "value"])
    df = df.groupby(["date", "metric"], as_index=False).agg(value=("value", "last"))
    piv = df.pivot(index="date", columns="metric", values="value").reset_index()
    piv = piv.tail(days)
    return {
        "data": {"dates": piv["date"].tolist(),
                 "fin_balance": piv.get("margin_fin_balance", pd.Series(dtype=float)).tolist(),
                 "fin_buy": piv.get("margin_fin_buy", pd.Series(dtype=float)).tolist()},
        "as_of": str(piv["date"].iloc[-1]) if len(piv) else None,
        "coverage": f"{len(piv)}个交易日(上交所口径)",
        "denominator": "交易所披露原值(元)",
        "warnings": ["仅上交所；深交所逐日接口待并入"],
        "published_cutoff": str(piv["date"].iloc[-1]) if len(piv) else None,
    }


SW_INDUSTRIES = ["801010", "801030", "801040", "801050", "801080", "801110", "801120",
                 "801130", "801140", "801150", "801160", "801170", "801180", "801200",
                 "801210", "801230", "801710", "801720", "801730", "801740", "801750",
                 "801760", "801770", "801780", "801790", "801880", "801890", "801950",
                 "801960", "801970", "801980"]


@app.get("/api/v1/scenarios/compare")
def get_scenarios(fund: str = Query(..., min_length=6, max_length=6)):
    """FACT (disclosure) vs L1 (continuation) vs L2 (constrained regression)."""
    from models.nowcast import nowcast_fund
    with _session() as s:
        disc = s.execute(
            select(FactObservation.asset_key, FactObservation.value,
                   FactObservation.effective_at)
            .where(FactObservation.subject_key == f"fund:{fund}",
                   FactObservation.metric == "fund_industry_weight",
                   FactObservation.quality_status == "valid")
            .order_by(FactObservation.effective_at.desc())).all()
        l2 = nowcast_fund(s, fund, SW_INDUSTRIES, prior=None, window=120)
    if not disc and l2.get("status") != "ok":
        raise HTTPException(404, f"no data for fund {fund}")
    latest_q = max((d[2] for d in disc), default=None)
    fact_rows = [{"industry": a.split(":", 1)[-1], "weight": v}
                 for a, v, q in disc if q == latest_q and v > 0]
    l1 = {
        "scenario": "L1_cautious",
        "industry_exposure": {r["industry"]: round(r["weight"], 4) for r in fact_rows},
        "range_type": "identification_bound",
        "notes": "持仓延续：最近披露行业配置直接外推；未披露行业不设零",
        "invalidation": "新披露到达或基金类型变更",
    }
    return {
        "data": {
            "fund": fund,
            "FACT_disclosure": {"period": latest_q,
                                "industry_exposure": {r["industry"]: round(r["weight"], 4) for r in fact_rows}},
            "L1_cautious": l1,
            "L2_baseline": l2 if l2.get("status") == "ok" else {"status": l2.get("status")},
            "L3_extended": {"status": "not_generated",
                            "reason": "L3网格需要完整持仓披露样本（证据不足不生成，设计sec.8）"},
        },
        "as_of": l2.get("last_date") or latest_q,
        "coverage": f"披露{len(fact_rows)}行业@{latest_q}；L2窗口{l2.get('window_days')}日",
        "denominator": "披露口径=证监会大类行业；L2=申万一级近似（两口径并列不混算）",
        "warnings": l2.get("quality_flags", []),
        "published_cutoff": latest_q,
    }


@app.get("/api/v1/diffusion")
def get_diffusion(window: int = Query(20, ge=5, le=120)):
    from scenarios.engine import industry_diffusion
    with _session() as s:
        v = industry_diffusion(s, window)
    return v


@app.get("/api/v1/estimate/aggregate")
def get_aggregate(refresh: bool = Query(False, description="重算（60基金回归约2分钟）")):
    import json as _json
    snap = ROOT / "data" / "p0_results" / "aggregate_snapshot.json"
    if refresh or not snap.exists():
        from models.aggregate import aggregate_exposure
        with _session() as s:
            out = aggregate_exposure(s, window=120)
        out["as_of"] = dt.datetime.now().isoformat(timespec="seconds")
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(_json.dumps(out, ensure_ascii=False, indent=1))
        return out
    out = _json.loads(snap.read_text())
    out["cached"] = True
    return out


@app.get("/api/v1/sectors/panels")
def get_sector_panels(top: int = Query(40, ge=1, le=100)):
    """Major-industry group panels: main/fund/etf/margin lines per group."""
    import json as _json
    import numpy as np
    from models.sector_panel import sector_panel
    from storage.models import Asset, AssetMembership
    from sqlalchemy.orm import aliased
    cache_file = ROOT / "data" / "p0_results" / "timeseries_cache.json"
    ts = _json.loads(cache_file.read_text()) if cache_file.exists() else {}
    groups = _json.loads((ROOT / "configs" / "major_groups.json").read_text())
    tdx_idx = _json.loads((ROOT / "configs" / "tdx_group_index.json").read_text())
    with _session() as s:
        assets = dict(s.execute(select(Asset.asset_key, Asset.name)
                                .where(Asset.asset_type == "em_board")).all())
        name2bk: dict[str, str] = {nm: k for k, nm in assets.items()}
        ma, ga = aliased(Asset), aliased(Asset)
        member_rows = s.execute(
            select(ga.asset_key, ma.asset_key)
            .join(AssetMembership, AssetMembership.group_asset_id == ga.id)
            .join(ma, AssetMembership.asset_id == ma.id)
            .where(ga.asset_key.like("em_board:%"))).all()
        members_by_board: dict[str, set] = {}
        for gk, sk in member_rows:
            members_by_board.setdefault(gk.split(":", 1)[1], set()).add(sk)

        panels = []
        for gname, spec in list(groups.items())[:top]:
            # main-force: sum flow over NON-NESTED anchor boards chosen in config
            main_bks = [name2bk[n] for n in spec["main"] if n in name2bk]
            flow_by_date: dict[str, float] = {}
            rows = s.execute(
                select(FactObservation.subject_key, FactObservation.effective_at,
                       FactObservation.value)
                .where(FactObservation.metric == "sf_main_net",
                       FactObservation.subject_key.in_([f"bk:{k}" for k in main_bks]),
                       FactObservation.quality_status == "valid",
                       FactObservation.effective_at >= f"{dt.date.today().year}-01-01")
                .order_by(FactObservation.effective_at)).all()
            for _bk, d, v in rows:
                flow_by_date[d] = flow_by_date.get(d, 0.0) + float(v) / 1e8
            flow = sorted(flow_by_date.items())
            cum, run = [], 0.0
            for d, v in flow:
                run += v
                cum.append((d, round(run, 2)))
            episodes = []
            if len(flow) >= 30:
                vals = np.array([v for _, v in flow])
                roll = np.convolve(vals, np.ones(20), mode="valid")
                i_max, i_min = int(np.argmax(roll)), int(np.argmin(roll))
                episodes = [
                    {"type": "加仓", "from": flow[i_max][0], "to": flow[i_max + 19][0],
                     "net_yi": round(float(roll[i_max]), 1)},
                    {"type": "出货", "from": flow[i_min][0], "to": flow[i_min + 19][0],
                     "net_yi": round(float(roll[i_min]), 1)},
                ]

            # margin: union of member STOCKS across boards (dedup at stock level)
            stocks: set = set()
            for n in spec.get("margin", []):
                stocks |= members_by_board.get(n, set())
            margin_by_date: dict[str, float] = {}
            if stocks:
                stock_list = sorted(stocks)
                for i in range(0, len(stock_list), 900):
                    chunk = stock_list[i:i + 900]
                    mrows = s.execute(
                        select(FactObservation.effective_at, FactObservation.value)
                        .where(FactObservation.metric == "margin_fin_balance",
                               FactObservation.subject_key.in_(chunk),
                               FactObservation.quality_status == "valid")).all()
                    for d, v in mrows:
                        margin_by_date[d] = margin_by_date.get(d, 0.0) + float(v) / 1e8
            margin_pts = [(d, round(v, 1)) for d, v in sorted(margin_by_date.items())]

            # fund / etf: sum precomputed SW-level series over the group's SW set
            def _sw_sum(key: str) -> list:
                vals: dict[str, float] = {}
                for swc in spec.get("sw", []):
                    for d, v in (ts.get(key) or {}).get(swc, []):
                        vals[d] = vals.get(d, 0.0) + float(v)
                return sorted((d, round(v, 4)) for d, v in vals.items())

            fund_pts = _sw_sum("fund_exp")
            etf_pts = _sw_sum("etf_state")

            # TDX board-index price line: full-year context incl. Jan-Mar
            px_pts, amt_pts = [], []
            for code in tdx_idx.get(gname, []):
                rows = s.execute(
                    select(FactObservation.effective_at, FactObservation.metric,
                           FactObservation.value)
                    .where(FactObservation.subject_key == f"tdx880:{code}",
                           FactObservation.metric.in_(("tdx_close", "tdx_amount")),
                           FactObservation.quality_status == "valid")
                    .order_by(FactObservation.effective_at)).all()
                closes = {d: float(v) for d, m, v in rows if m == "tdx_close"}
                amts = {d: float(v) for d, m, v in rows if m == "tdx_amount"}
                for d, v in closes.items():
                    px_pts.append((d, v))
                for d, v in amts.items():
                    amt_pts.append((d, v / 1e8))
            # average when multiple index codes map to one group
            def _avg(points):
                acc: dict[str, list] = {}
                for d, v in points:
                    acc.setdefault(d, []).append(v)
                return sorted((d, round(sum(x) / len(x), 2)) for d, x in acc.items())
            px_series = _avg(px_pts)
            if px_series:
                base_px = px_series[0][1] or 1.0
                px_norm = [(d, round((v / base_px - 1) * 100, 2)) for d, v in px_series]
            else:
                px_norm = []
            amt_series = _avg(amt_pts)

            gross = sum(abs(v) for _, v in flow)
            net = sum(v for _, v in flow)
            notes = []
            if flow:
                notes.append(f"年内主力净流入 累计{net:+.0f}亿（毛额{gross:.0f}亿，单日买卖对冲不可见）")
                notes.append(f"净/毛效率 {net / gross:+.2f}（-1持续出货 … +1持续吸筹）" if gross else "无毛额数据")
            else:
                notes.append("主力历史补采中（数据源临时限流）")
            if margin_pts:
                notes.append(f"融资余额 {margin_pts[0][1]:.0f}亿→最新{margin_pts[-1][1]:.0f}亿（Δ{margin_pts[-1][1] - margin_pts[0][1]:+.0f}亿）")
            if fund_pts:
                notes.append(f"公募暴露 年初{fund_pts[0][1] * 100:.1f}%→现在{fund_pts[-1][1] * 100:.1f}%（Δ{(fund_pts[-1][1] - fund_pts[0][1]) * 100:+.1f}pp）")
            if etf_pts:
                notes.append(f"ETF等效持仓 {etf_pts[0][1]:.0f}亿→{etf_pts[-1][1]:.0f}亿")
            if px_norm:
                notes.append(f"板块指数 年初至今{px_norm[-1][1]:+.1f}%（1-3月由通达信本地数据覆盖）")

            panels.append({
                "board": "GROUP:" + gname, "name": gname, "sw": ",".join(spec.get("sw", [])),
                "lines": {
                    "main_cum": {"points": cum, "unit": "亿元", "scenario": "FACT_PROXY",
                                 "label": "主力大单累计净流入"},
                    "fund_exp": {"points": fund_pts, "unit": "%", "scenario": "L2",
                                 "label": "公募行业暴露(月度)"},
                    "etf_state": {"points": etf_pts, "unit": "亿元", "scenario": "L2_ASSUMPTION",
                                  "label": "宽基ETF等效持仓(季度)"},
                    "margin_bal": {"points": margin_pts, "unit": "亿元", "scenario": "FACT",
                                   "label": "板块融资余额"},
                    "board_px": {"points": px_norm, "unit": "%", "scenario": "FACT",
                                 "label": "板块指数年内涨跌(TDX)"},
                    "board_amt": {"points": amt_series, "unit": "亿元", "scenario": "FACT",
                                  "label": "板块成交额(TDX)"},
                },
                "episodes": episodes,
                "cost_notes": notes,
                "assumptions": sector_panel(s, "BK1036")["assumptions"],  # static sidebar
                "as_of": max([d for d, _ in cum] + [d for d, _ in margin_pts], default=None),
                "coverage": f"主力{len(cum)}日；两融{len(margin_pts)}时点；公募{len(fund_pts)}点；ETF{len(etf_pts)}点",
                "warnings": [],
            })
    return {"data": panels, "as_of": max((p["as_of"] for p in panels if p["as_of"]), default=None),
            "coverage": f"{len(panels)}个主要行业板块",
            "denominator": "每线口径见 assumptions；主力=大单代理；公募/ETF=估计或假设线",
            "warnings": ["主力线为L1分单代理非识别机构；ETF线为季度点假设折算"]}


@app.get("/api/v1/sectors/{bk}/panel")
def get_one_panel(bk: str):
    import json as _json
    from models.sector_panel import sector_panel
    cache_file = ROOT / "data" / "p0_results" / "timeseries_cache.json"
    ts = _json.loads(cache_file.read_text()) if cache_file.exists() else {}
    with _session() as s:
        v = sector_panel(s, bk, fund_expo_ts=ts.get("fund_exp"),
                         etf_equiv_ts=ts.get("etf_state"))
    return v


@app.get("/api/v1/quality/status")
def get_quality():
    with _session() as s:
        per_metric = s.execute(
            select(FactObservation.metric, FactObservation.subject_key,
                   func.count(), func.min(FactObservation.effective_at),
                   func.max(FactObservation.effective_at))
            .group_by(FactObservation.metric).order_by(func.count().desc())
        ).all()
        jobs = s.execute(
            select(JobRun.job_key, JobRun.status, JobRun.finished_at, JobRun.data_range)
            .order_by(JobRun.id.desc()).limit(20)).all()
    return {
        "data": {
            "facts_by_metric": [
                {"metric": m, "sample_subject": subj, "rows": n, "range": f"{lo}..{hi}"}
                for m, subj, n, lo, hi in per_metric],
            "recent_jobs": [
                {"job_key": k, "status": st, "finished_at": str(f), "range": r}
                for k, st, f, r in jobs],
        },
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "coverage": f"{sum(r[2] for r in per_metric)} facts",
        "denominator": "fact_observation 计数（按修订链去重前）",
        "warnings": [],
    }


@app.get("/api/v1/etf/shares")
def get_etf_shares(code: str = Query(..., min_length=6, max_length=6),
                   days: int = Query(30, ge=1, le=500)):
    """ETF 日度官方总份额（沪深交易所直连事实，T+1可得）。

    share_change 严格同源差分；estimated_net_flow = Δ份额 × 当日净值
    （净值缺失则为 null，不外推）。两所口径与时点不同，不跨源合并。
    """
    rows = engine.connect().execute(
        select(FactObservation.source_id, FactObservation.effective_at,
               FactObservation.value)
        .where(FactObservation.asset_key == code,
               FactObservation.metric == "etf_total_shares",
               FactObservation.quality_status == "valid")
        .order_by(FactObservation.effective_at.desc())).all()
    if not rows:
        raise HTTPException(404, f"no etf_total_shares facts for {code}")
    nav_rows = engine.connect().execute(
        select(FactObservation.effective_at, FactObservation.value)
        .where(FactObservation.asset_key == code,
               FactObservation.metric == "etf_nav",
               FactObservation.quality_status == "valid")
        .order_by(FactObservation.effective_at.desc())).all()
    nav_map = {d: float(v) for d, v in nav_rows}
    by_source: dict[str, list[tuple[str, float]]] = {}
    for src, d, v in rows:
        by_source.setdefault(src, []).append((d, float(v)))
    series = []
    for src, items in sorted(by_source.items()):
        items.sort()  # 升序便于差分
        prev: float | None = None
        src_series = []
        for d, v in items:
            change = (v - prev) if prev is not None else None
            nav = nav_map.get(d)
            est = (change * nav) if (change is not None and nav) else None
            src_series.append({"date": d, "source": src, "total_share": v,
                               "share_change": change,
                               "nav": nav,
                               "estimated_net_flow": est})
            prev = v
        series.extend(src_series[-days:])  # 全序列差分后再截断窗口
    series.sort(key=lambda r: r["date"], reverse=True)
    latest = max(d for _, d, _ in rows)
    return {
        "code": code,
        "as_of": latest,
        "unit": {"total_share": "shares", "share_change": "shares",
                 "estimated_net_flow": "CNY"},
        "series": series,
        "evidence": "交易所官方日度份额（T日晚清算后发布；share_change同源差分；"
                    "estimated_net_flow=Δ×当日净值，净值缺失不外推）",
        "warnings": ["沪深两所发布时点不同，不跨源差分", "当日数据T+1早可得，盘中无此事实"],
    }


@app.get("/api/v1/context")
def get_context(code: str | None = Query(None, description="股票代码（展示用）"),
                board: str = Query(None, description="EM板块BK代码，如BK0420"),
                as_of: str | None = Query(None, description="PIT截止时间ISO-8601")):
    """决策系统上下文契约 v0.2（market/sector 两层）。

    市场层（fact）：
      - market:broad_etf_creation：宽基ETF净创设/赎回价值 net_creation_value
        = Σ(Δshares × NAV)，同源差分；NAV 缺失回退链 etf_nav(≤date) → etf_close；
        它代表新增 ETF 暴露规模（实物申赎），不等价于二级市场现金买股金额；
      - market:margin_sse / market:margin_szse：沪深两融分通道（不强相加，判同向）。
    行业层：
      - sector:em_board_flow=proxy（唯一实装；board_margin/industry_etf 为规划位，
        计入 coverage 分母以诚实呈现缺口）。
    context 规则：板块方向 × 市场净偏向（一致流入=supportive，任何方向相左=divergent，
    其余=neutral）；无板块/缺数据 → unknown（绝不默认 neutral）。
    coverage 分母 = 预期通道总数（缺失也计入）。
    """

    def _iso(d: str | None) -> str | None:
        if not d:
            return d
        d = str(d).replace("-", "")
        return f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else str(d)

    try:
        cutoff = dt.datetime.fromisoformat(as_of) if as_of else dt.datetime.utcnow()
    except ValueError as exc:
        raise HTTPException(400, "as_of must be ISO-8601") from exc
    cutoff_iso = cutoff.replace(microsecond=0).isoformat()
    conn = engine.connect()
    channels: dict[str, dict] = {}
    limitations: list[str] = []

    # ---- 市场层 1：宽基 ETF 净创设价值 Σ(Δshares×NAV) ----
    broad = ("510300", "510500", "159915", "588000")
    srows = conn.execute(
        select(FactObservation.asset_key, FactObservation.source_id,
               FactObservation.effective_at, FactObservation.value,
               FactObservation.available_at)
        .where(FactObservation.asset_key.in_(broad),
               FactObservation.metric == "etf_total_shares",
               FactObservation.quality_status == "valid",
               FactObservation.available_at.is_not(None),
               FactObservation.available_at <= cutoff_iso)
        .order_by(FactObservation.effective_at.desc())).all()
    per: dict[tuple[str, str], list[tuple[str, float, str | None]]] = {}
    for akey, src, d, v, available in srows:
        per.setdefault((akey, src), []).append((str(d), float(v), available))

    def _nav_at(akey: str, date_key: str) -> tuple[float, str] | None:
        """NAV 回退链：etf_nav(≤date 最新) → etf_close(≤date 最新) → None。"""
        for metric, tag in (("etf_nav", "nav"), ("etf_close", "close_proxy")):
            r = conn.execute(
                select(FactObservation.effective_at, FactObservation.value)
                .where(FactObservation.asset_key == akey,
                       FactObservation.metric == metric,
                       FactObservation.quality_status == "valid",
                       FactObservation.effective_at <= date_key,
                       FactObservation.available_at.is_not(None),
                       FactObservation.available_at <= cutoff_iso)
                .order_by(FactObservation.effective_at.desc()).limit(1)).first()
            if r and float(r[1] or 0) > 0:
                return float(r[1]), tag
        return None

    per_etf: list[dict] = []
    net_creation_value = 0.0
    latest_dates: list[str] = []
    for (akey, src), items in sorted(per.items()):
        items.sort()
        if len(items) < 2:
            continue
        d_new, v_new, available_new = items[-1]
        d_old, v_old, _ = items[-2]
        if not available_new:
            limitations.append(f"{akey}:availability_unknown")
            continue
        delta_shares = v_new - v_old
        navr = _nav_at(akey, d_new)
        if navr is None:
            limitations.append(f"{akey}:no_nav_or_close_fallback")
            continue
        nav_used, nav_src = navr
        value = delta_shares * nav_used
        net_creation_value += value
        latest_dates.append(d_new)
        per_etf.append({"code": akey, "source": src, "date": _iso(d_new),
                        "delta_shares": round(delta_shares, 0),
                        "nav_used": round(nav_used, 4), "nav_source": nav_src,
                        "net_creation_value_yuan": round(value, 0)})
    etf_dir = None
    if per_etf:
        etf_dir = "inflow" if net_creation_value > 0 else ("outflow" if net_creation_value < 0 else "flat")
        channels["market:broad_etf_creation"] = {
            "evidence_type": "fact", "observation_date": _iso(max(latest_dates)),
            "available_at": max(x[2] for items in per.values() for x in items if x[2]),
            "status": "fresh",
            "direction": etf_dir, "net_creation_value_yuan": round(net_creation_value, 0),
            "per_etf": per_etf,
            "note": "净创设/赎回价值=Σ(Δ份额×NAV)，T-1官方份额事实，同源差分；"
                    "实物申赎口径，不等于二级现金买股金额",
        }
    else:
        limitations.append("broad_etf_creation_no_data")

    # ---- 市场层 2：沪深两融分通道（fact，判断同向不强加） ----
    margin_dirs: dict[str, str] = {}
    for mkt, ch_name in (("market:SSE", "market:margin_sse"), ("market:SZSE", "market:margin_szse")):
        mrows = conn.execute(
            select(FactObservation.effective_at, FactObservation.value,
                   FactObservation.available_at)
            .where(FactObservation.subject_key == mkt,
                   FactObservation.metric == "margin_fin_balance",
                   FactObservation.quality_status == "valid",
                   FactObservation.available_at.is_not(None),
                   FactObservation.available_at <= cutoff_iso)
            .order_by(FactObservation.effective_at.desc()).limit(10)).all()
        if len(mrows) >= 6 and mrows[0][2]:
            delta5 = float(mrows[0][1]) - float(mrows[5][1])
            mdir = "inflow" if delta5 > 0 else "outflow"
            margin_dirs[mkt] = mdir
            channels[ch_name] = {
                "evidence_type": "fact", "observation_date": _iso(str(mrows[0][0])),
                "available_at": mrows[0][2], "status": "fresh",
                "direction": mdir, "delta5_yuan": delta5,
            }
        else:
            limitations.append(f"{ch_name}:insufficient")
    margin_alignment = None
    if len(margin_dirs) == 2:
        margin_alignment = ("same_direction" if margin_dirs["market:SSE"] == margin_dirs["market:SZSE"]
                            else "opposite")

    # 市场净偏向：etf + sse + szse 三通道符号合计
    bias_score = sum(1 if d == "inflow" else (-1 if d == "outflow" else 0)
                     for d in [etf_dir, *margin_dirs.values()] if d is not None)
    market_bias = ("inflow" if bias_score > 0 else "outflow" if bias_score < 0 else None) \
        if (etf_dir is not None or margin_dirs) else None

    # ---- 行业层 ----
    board_flow_dir = None
    if board:
        rows = conn.execute(
            select(FactObservation.effective_at, FactObservation.value,
                   FactObservation.available_at)
            .where(FactObservation.subject_key == f"bk:{board}",
                   FactObservation.metric == "sf_main_net",
                   FactObservation.quality_status == "valid",
                   FactObservation.available_at.is_not(None),
                   FactObservation.available_at <= cutoff_iso)
            .order_by(FactObservation.effective_at.desc()).limit(10)).all()
        if rows and rows[0][2]:
            cum5 = sum(v for _, v, _ in rows[:5])
            board_flow_dir = "inflow" if cum5 > 0 else ("outflow" if cum5 < 0 else "flat")
            channels["sector:em_board_flow"] = {
                "evidence_type": "proxy", "observation_date": _iso(str(rows[0][0])),
                "available_at": rows[0][2], "status": "fresh",
                "direction": board_flow_dir, "cum5_net": round(cum5, 0),
            }
        else:
            limitations.append("sector:em_board_flow:no_data_for_board")
    else:
        limitations.append("stock_board_mapping_not_configured: pass board=BKxxxx")

    # ---- context 规则（板块方向 × 市场净偏向） ----
    if board_flow_dir is None:
        context = "unknown"
    elif board_flow_dir == "flat":
        context = "neutral"
    elif market_bias is None:
        context = "unknown"
        limitations.append("market_bias_unavailable")
    elif (board_flow_dir == "inflow") == (market_bias == "inflow"):
        context = "supportive" if board_flow_dir == "inflow" else "neutral"
    else:
        context = "divergent"

    expected = ["market:broad_etf_creation", "market:margin_sse", "market:margin_szse",
                "sector:em_board_flow", "sector:board_margin", "sector:industry_etf"]
    known = sum(1 for ch in channels.values() if ch["status"] != "unknown")
    return {
        "sector_id": board,
        "symbol": code,
        "as_of": cutoff_iso,
        "latest_observation_date": max([ch["observation_date"] for ch in channels.values()], default=None),
        "context": context,
        "coverage": f"{known}/{len(expected)}",
        "channels": channels,
        "market_capital_context": {
            "bias": market_bias, "bias_score": bias_score,
            "broad_etf_net_creation_value_yuan": round(net_creation_value, 0),
            "margin_alignment": margin_alignment,
            "note": "市场层=资金整体环境（宽基ETF创设/赎回 + 沪深两融分立）",
        },
        "sector_capital_context": {
            "sector_id": board, "board_flow_direction": board_flow_dir,
            "expected_channels": ["sector:em_board_flow", "sector:board_margin",
                                  "sector:industry_etf"],
            "implemented": ["sector:em_board_flow"],
            "note": "行业层当前仅大单proxy；board_margin/industry_etf 为规划位",
        },
        "method_version": "context-v0.3-pit",
        "limitations": limitations,
        "notes": ["context为规则化输出：proxy与fact分级标注，缺数据=unknown非neutral",
                  "net_creation_value口径：实物申赎（新增ETF暴露规模），非二级现金买股",
                  "盘中可得性：em_flow当日盘后；margin/etf_shares为T/T-1事实"],
    }


@app.get("/api/v1/observations")
def get_observations(metric: str, start: str, end: str,
                     as_of: str | None = None, subject_key: str | None = None,
                     limit: int = Query(5000, ge=1, le=20000)):
    """Strict PIT fact window; unknown availability is excluded, never guessed."""
    try:
        cutoff = dt.datetime.fromisoformat(as_of) if as_of else dt.datetime.utcnow()
    except ValueError as exc:
        raise HTTPException(400, "as_of must be ISO-8601") from exc
    cutoff_iso = cutoff.replace(microsecond=0).isoformat()
    q = select(FactObservation).where(
        FactObservation.metric == metric,
        FactObservation.effective_at >= start,
        FactObservation.effective_at <= end,
        FactObservation.available_at.is_not(None),
        FactObservation.available_at <= cutoff_iso,
        FactObservation.quality_status == "valid")
    if subject_key:
        q = q.where(FactObservation.subject_key == subject_key)
    rows = Session(engine).execute(q.order_by(FactObservation.effective_at,
                                               FactObservation.id).limit(limit)).scalars().all()
    return {"metric": metric, "as_of": cutoff_iso, "view": "strict_pit",
            "count": len(rows), "observations": [{
                "id": r.id, "subject_key": r.subject_key, "asset_key": r.asset_key,
                "value": r.value, "unit": r.unit, "currency": r.currency,
                "effective_at": r.effective_at, "published_at": r.published_at,
                "available_at": r.available_at,
                "ingested_at": r.ingested_at.isoformat() if r.ingested_at else None,
                "source": r.source_id, "revision": r.revision_id,
                "supersedes_id": r.supersedes_id, "quality": r.quality_status,
            } for r in rows],
            "limitations": ["unknown available_at excluded",
                            "hindsight/revised-complete view not mixed into this endpoint"]}


class ContextItem(BaseModel):
    code: str | None = None
    board: str | None = None


class ContextBatchRequest(BaseModel):
    as_of: str
    items: list[ContextItem]


@app.post("/api/v1/context/batch")
def get_context_batch(req: ContextBatchRequest):
    if len(req.items) > 500:
        raise HTTPException(400, "maximum 500 context items")
    return {"as_of": req.as_of, "method_version": "context-v0.3-pit",
            "items": [get_context(x.code, x.board, req.as_of) for x in req.items]}


@app.get("/health")
def health():
    return {"ok": True}
