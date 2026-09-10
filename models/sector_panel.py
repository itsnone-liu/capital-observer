"""Sector panel (user-facing core view): one board, several capital lines,
YTD, with accumulation/distribution episodes highlighted and cost notes.

Lines per board (each with explicit basis + scenario tag):
  main_cum    主力大单累计净流入(亿元) — FACT-level proxy (L1 auction buckets)
  fund_exp    公募行业暴露(L2估计, 月度点) — constrained regression cohort
  etf_state   宽基ETF规模折算行业等效持仓(亿元, 季度) — L2 assumption
              (ETF份额×净值×指数行业权重；"国家队/配置盘"代理)
  margin_bal  板块融资余额(亿元, 明细日点) — FACT (dual-exchange detail)

Episodes: top-2 accumulation & top-2 distribution windows from 20d rolling
main-force flow extremes. Cost notes: gross flow, efficiency, fund-rebalance
impact vs board ADV (when available).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from storage.models import Asset, FactObservation

ROOT = Path(__file__).resolve().parents[1]
MAJOR = json.loads((ROOT / "configs" / "major_boards.json").read_text())
MAJOR_NAMES = set(MAJOR["exact"])
SW_EM = json.loads((ROOT / "configs" / "sw_em_map.json").read_text())
EM_SW = {}
for sw, ems in SW_EM.items():
    if sw.startswith("_"):
        continue
    for e in ems:
        EM_SW[e] = sw

BROAD_ETFS = {"510300": "000300", "510050": "000016", "510500": "000905",
              "512100": "000852", "563300": "000905", "588000": "000688"}


def _ytd() -> str:
    return f"{dt.date.today().year}-01-01"


def _series(session: Session, subject: str, metric: str, start: str) -> list[tuple[str, float]]:
    rows = session.execute(
        select(FactObservation.effective_at, FactObservation.value, FactObservation.id)
        .where(FactObservation.subject_key == subject,
               FactObservation.metric == metric,
               FactObservation.quality_status.in_(("valid", "degraded")),
               FactObservation.effective_at >= start)
        .order_by(FactObservation.effective_at, FactObservation.id)).all()
    seen: dict[str, float] = {}
    for d, v, _ in rows:
        seen[d] = float(v)
    return sorted(seen.items())


def board_universe(session: Session) -> list[dict]:
    """All EM boards with main-flow data + SW industry mapping."""
    names = dict(session.execute(
        select(Asset.asset_key, Asset.name).where(Asset.asset_type == "em_board")).all())
    rows = session.execute(
        select(FactObservation.subject_key)
        .where(FactObservation.metric == "sf_main_net",
               FactObservation.quality_status == "valid",
               FactObservation.effective_at >= _ytd())).all()
    days: dict[str, int] = {}
    for (s,) in rows:
        bk = s.split(":", 1)[-1]
        days[bk] = days.get(bk, 0) + 1
    out = []
    for bk, n in days.items():
        nm = names.get(bk, "")
        out.append({"board": bk, "name": nm, "days": n,
                    "sw": EM_SW.get(nm.replace("行业", "").replace("Ⅱ", ""), "")})
    # Main view uses the user's major-board whitelist; tiny sub-industries stay hidden.
    major = [x for x in out if x["name"] in MAJOR_NAMES]
    major.sort(key=lambda x: -x["days"])
    return major or sorted(out, key=lambda x: -x["days"])


def sector_panel(session: Session, bk: str, fund_expo_ts: dict | None = None,
                 etf_equiv_ts: dict | None = None) -> dict:
    """Assemble one board's panel. fund_expo_ts/etf_equiv_ts are precomputed
    SW-level dicts {industry: [(date, value)...]} passed in bulk for speed."""
    name_row = session.execute(
        select(Asset.name).where(Asset.asset_key == bk)).scalar()
    name = name_row or bk
    sw = EM_SW.get(name.replace("行业", "").replace("Ⅱ", ""), "")

    # -- line 1: main-force cumulative (亿元) --
    flow = _series(session, f"bk:{bk}", "sf_main_net", _ytd())
    cum, run = [], 0.0
    for d, v in flow:
        run += v / 1e8
        cum.append((d, round(run, 2)))

    # episodes: 20d rolling sum extremes
    episodes = []
    if len(flow) >= 30:
        vals = np.array([v for _, v in flow])
        roll = np.convolve(vals, np.ones(20) / 1e8, mode="valid")  # 20d sum in 亿
        idx_max = int(np.argmax(roll)); idx_min = int(np.argmin(roll))
        episodes = [
            {"type": "加仓", "from": flow[idx_max][0], "to": flow[idx_max + 19][0],
             "net_yi": round(float(roll[idx_max]), 1)},
            {"type": "出货", "from": flow[idx_min][0], "to": flow[idx_min + 19][0],
             "net_yi": round(float(roll[idx_min]), 1)},
        ]

    # -- line 4: margin balance (stock detail joined via em_board membership) --
    from storage.models import AssetMembership
    from sqlalchemy.orm import aliased
    ma, ga = aliased(Asset), aliased(Asset)
    members = session.execute(
        select(ma.asset_key).join(AssetMembership, AssetMembership.asset_id == ma.id)
        .join(ga, AssetMembership.group_asset_id == ga.id)
        .where(ga.asset_key == f"em_board:{name}")).scalars().all()
    # Asset.asset_key already uses stock:XXXX namespace.
    margin_series: dict[str, float] = {}
    if members:
        rows = session.execute(
            select(FactObservation.subject_key, FactObservation.effective_at,
                   FactObservation.value)
            .where(FactObservation.metric == "margin_fin_balance",
                   FactObservation.subject_key.in_(members),
                   FactObservation.quality_status == "valid")).all()
        for subj, d, v in rows:
            margin_series[d] = margin_series.get(d, 0.0) + float(v) / 1e8
    margin_pts = sorted(margin_series.items())

    # -- cost notes --
    gross = sum(abs(v) for _, v in flow) / 1e8
    net = sum(v for _, v in flow) / 1e8
    cost_notes = [
        f"年内主力净流入 累计{net:+.0f}亿（毛额{gross:.0f}亿，单日买卖对冲不可见）",
        f"净/毛效率 {net / gross:+.2f}（-1持续出货 … +1持续吸筹）" if gross else "无毛额数据",
        f"融资余额 最新{margin_pts[-1][1]:.0f}亿" if margin_pts else "融资余额无数据",
    ]
    if sw and fund_expo_ts and sw in fund_expo_ts:
        e_now = fund_expo_ts[sw][-1][1] if fund_expo_ts[sw] else None
        e_0 = fund_expo_ts[sw][0][1] if fund_expo_ts[sw] else None
        if e_now is not None and e_0 is not None:
            cost_notes.append(
                f"公募暴露 年初{e_0 * 100:.1f}%→现在{e_now * 100:.1f}%（Δ{(e_now - e_0) * 100:+.1f}pp）")

    # -- assumptions sidebar --
    assumptions = [
        ["主力(大单)", "FACT代理", "大单+超大单净额，L1分单口径；是活跃资金代理，不识别持牌机构"],
        ["公募暴露", "L2估计", f"申万一级{'（' + sw + '）' if sw else '(未映射)'}→本板块等权折算；约束回归月度点，样本62只无规模权重"],
        ["ETF配置盘", "L2假设", "宽基ETF(300/50/500/1000/科创50)季度份额×净值×行业权重折算；假设全部视为配置盘上限（含国家队情景）"],
        ["两融余额", "FACT", "两融标的明细join板块成分；非全板块市值"],
    ]
    return {
        "board": bk, "name": name, "sw": sw,
        "lines": {
            "main_cum": {"points": cum, "unit": "亿元", "scenario": "FACT_PROXY",
                         "label": "主力大单累计净流入"},
            "fund_exp": {"points": (fund_expo_ts or {}).get(sw, []) if sw else [],
                         "unit": "%", "scenario": "L2",
                         "label": "公募行业暴露(月度)"},
            "etf_state": {"points": (etf_equiv_ts or {}).get(sw, []) if sw else [],
                          "unit": "亿元", "scenario": "L2_ASSUMPTION",
                          "label": "宽基ETF等效持仓(季度)"},
            "margin_bal": {"points": [(d, round(v, 1)) for d, v in margin_pts],
                           "unit": "亿元", "scenario": "FACT",
                           "label": "板块融资余额"},
        },
        "episodes": episodes,
        "cost_notes": cost_notes,
        "assumptions": assumptions,
        "as_of": max([d for d, _ in cum], default=None),
        "coverage": f"主力{len(cum)}日；两融{len(margin_pts)}个时点；公募{'有' if sw and fund_expo_ts and sw in fund_expo_ts else '无'}映射",
        "warnings": [],
    }


def fund_exposure_timeseries(session: Session, month_step: int = 1) -> dict:
    """Monthly public-fund SW exposure points YTD (L2). ~9 regressions x funds."""
    from models.nowcast import nowcast_fund
    from models.aggregate import SW_INDUSTRIES, fund_universe
    funds = fund_universe(session)
    # month-end reference dates: last trading day of each month YTD from nav facts
    nav_dates = session.execute(
        select(FactObservation.effective_at).distinct()
        .where(FactObservation.metric == "fund_nav")).scalars().all()
    months: dict[str, str] = {}
    for d in sorted(nav_dates):
        if len(d) == 10:
            months[d[:7]] = d  # last day present per month
    ref_dates = [d for m, d in sorted(months.items()) if d >= _ytd()][::month_step]
    out: dict[str, list[tuple[str, float]]] = {}
    for ref in ref_dates:
        agg: dict[str, list[float]] = {}
        for f in funds:
            end_idx_cutoff = ref
            try:
                o = nowcast_fund(session, f, SW_INDUSTRIES, prior=None, window=60, end_date=ref)
            except Exception:  # noqa: BLE001
                continue
            if o.get("status") != "ok" or o.get("last_date", "") > end_idx_cutoff:
                continue
            if (o.get("fit_r2_in_sample") or 0) < 0.2:
                continue
            for k, v in o["industry_exposure"].items():
                agg.setdefault(k, []).append(v)
        for k, vs in agg.items():
            out.setdefault(k, []).append((ref, round(sum(vs) / len(vs), 4)))
    return out


def etf_state_equivalent(session: Session) -> dict:
    """Broad-ETF size mapped to SW industries via CSI weights (quarterly)."""
    from storage.models import AssetMembership
    # stock -> EM board name -> SW via EM_SW
    name_of_stock: dict[str, str] = {}
    from sqlalchemy.orm import aliased
    ma, mga = aliased(Asset), aliased(Asset)
    for board_key, stock_key in session.execute(
            select(mga.asset_key, ma.asset_key)
            .join(AssetMembership, AssetMembership.group_asset_id == mga.id)
            .join(ma, AssetMembership.asset_id == ma.id)
            .where(mga.asset_key.like("em_board:%"))).all():
        name_of_stock[stock_key] = board_key.split(":", 1)[1]
    ci, cstock = aliased(Asset), aliased(Asset)
    wrows = session.execute(
        select(ci.asset_key, cstock.asset_key, AssetMembership.weight)
        .join(ci, AssetMembership.group_asset_id == ci.id)
        .join(cstock, AssetMembership.asset_id == cstock.id)
        .where(ci.asset_key.in_(list(set(BROAD_ETFS.values()))))).all()
    idx_ind_w: dict[str, dict[str, float]] = {}
    for subj, member, w in wrows:
        idx = subj
        stock = member if str(member).startswith("stock:") else f"stock:{member}"
        nm = name_of_stock.get(stock, "")
        sw = EM_SW.get(nm.replace("行业", "").replace("Ⅱ", ""), "")
        if sw and w:
            d = idx_ind_w.setdefault(idx, {})
            d[sw] = d.get(sw, 0.0) + float(w or 0)
    # ETF size series (share x nav x price basis: use nav as proxy)
    out: dict[str, list[tuple[str, float]]] = {}
    for code, idx in BROAD_ETFS.items():
        if idx not in idx_ind_w:
            continue
        shares = _series(session, f"etf:{code}", "etf_share_end", "2025-01-01")
        navs = dict(_series(session, f"etf:{code}", "etf_nav", "2025-01-01"))
        for d, s in shares:
            if d not in navs:
                continue
            size_yi = s * navs[d] / 1e8
            for sw, w in idx_ind_w[idx].items():
                out.setdefault(sw, []).append((d, round(size_yi * w, 1)))
    # Aggregate multiple broad ETFs on the same disclosure date before serving.
    # This prevents one quarter from appearing as a row of unrelated blue squares.
    for sw, pts in list(out.items()):
        by_date: dict[str, float] = {}
        for d, value in pts:
            by_date[d] = by_date.get(d, 0.0) + float(value)
        out[sw] = sorted((d, round(v, 1)) for d, v in by_date.items())
    return out
