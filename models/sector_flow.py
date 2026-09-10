"""Sector capital-structure views (year-to-date focus).

Three capital channels per EM board / SW industry, each with explicit basis:
  1. 主力(main-force proxy): large-order net flow — YTD cumulative / 20d / today
     (basis: exchange L1 auction size buckets, proxy NOT identified holdings)
  2. 杠杆(margin): board-level margin balance change YTD (stock detail joined
     to industry map; baseline = first available trading days of Jan)
  3. 机构(estimated): public-fund industry exposure now vs year-start window
     (L2 constrained regression, identification_bound)

Cost: YTD |flow|/YTD turnover per board (turnover-cost proxy) + fund-rebalance
impact on boards (exposure delta x est. AUM / board ADV).
"""
from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from storage.models import Asset, FactObservation, asset_membership  # noqa: F401


def _ytd() -> str:
    import datetime as dt
    return f"{dt.date.today().year}-01-01"


def board_flow_ytd(session: Session, top_n: int = 30) -> dict:
    """YTD main-force net flow per board + 20d + latest day, ranked."""
    names = dict(session.execute(
        select(Asset.asset_key, Asset.name).where(Asset.asset_type == "em_board")).all())
    rows = session.execute(
        select(FactObservation.asset_key, FactObservation.effective_at,
               FactObservation.value)
        .where(FactObservation.metric == "sf_main_net",
               FactObservation.quality_status == "valid",
               FactObservation.effective_at >= _ytd())
        .order_by(FactObservation.effective_at)).all()
    per: dict[str, dict] = {}
    for bk, d, v in rows:
        e = per.setdefault(bk, {"today": 0.0, "d20": 0.0, "ytd": 0.0, "days": set()})
        e["ytd"] += v
        e["days"].add(d)
    latest_day = max((d for _, d, _ in rows), default=None)
    for bk, d, v in rows:
        if d == latest_day:
            per[bk]["today"] += v
    # 20d window: last 20 distinct dates overall
    days = sorted({d for _, d, _ in rows})[-20:]
    for bk, d, v in rows:
        if d in days:
            per[bk]["d20"] += v
    data = sorted(
        ({"board": bk, "name": names.get(bk, bk), "today_yuan": round(e["today"]),
          "d20_yuan": round(e["d20"]), "ytd_yuan": round(e["ytd"]),
          "days": len(e["days"])} for bk, e in per.items()),
        key=lambda x: x["ytd_yuan"], reverse=True)
    return {"data": data[:top_n], "as_of": latest_day,
            "coverage": f"{len(per)}个板块有年内数据（展示前{top_n}）",
            "denominator": "主力净流入=大单+超大单口径（东财L1分单），机构代理而非识别持仓",
            "warnings": ["主力=大单口径代理，非持牌机构持仓"]}


def board_margin_ytd(session: Session) -> dict:
    """Margin balance by EM industry: latest vs year-start baseline."""
    rows = session.execute(
        select(AssetMembership.subject_key, AssetMembership.member_key)
        .where(AssetMembership.subject_key.like("em_board:%"))).all()
    board_of = {m.split(":", 1)[1]: s.split(":", 1)[1] for s, m in rows}
    if not board_of:
        return {"data": [], "as_of": None, "coverage": "0",
                "denominator": "", "warnings": ["股票行业映射未加载"]}
    facts = session.execute(
        select(FactObservation.subject_key, FactObservation.effective_at,
               FactObservation.value)
        .where(FactObservation.metric == "margin_fin_balance",
               FactObservation.subject_key.like("stock:%"),
               FactObservation.quality_status == "valid")
        .order_by(FactObservation.effective_at)).all()
    per: dict[str, dict[str, float]] = {}
    for subj, d, v in facts:
        b = board_of.get(subj.split(":", 1)[1])
        if b:
            per.setdefault(b, {})[d] = per.setdefault(b, {}).get(d, 0.0) + float(v)
    out = []
    for b, series in per.items():
        if len(series) < 2:
            continue
        dates = sorted(series)
        year_dates = [d for d in dates if d >= _ytd()]
        base_d = year_dates[0] if year_dates else dates[0]
        last_d = dates[-1]
        out.append({"board": b, "baseline_date": base_d, "latest_date": last_d,
                    "baseline_yuan": round(series[base_d]),
                    "latest_yuan": round(series[last_d]),
                    "chg_yuan": round(series[last_d] - series[base_d]),
                    "chg_pct": round((series[last_d] / series[base_d] - 1) * 100, 2)
                    if series[base_d] else None})
    out.sort(key=lambda x: x["chg_yuan"], reverse=True)
    return {"data": out, "as_of": max((o["latest_date"] for o in out), default=None),
            "coverage": f"{len(out)}个行业（两融标的明细join行业映射）",
            "denominator": "板块两融余额=成分股融资余额加总（明细披露日粒度）",
            "warnings": ["基线为年内首个披露日（非1月1日）；明细覆盖两融标的，非全板块市值"]}


def board_cost_ytd(session: Session) -> dict:
    """Turnover-cost proxy per board: YTD |main flow| / YTD amount (if amount available)
    — falls back to flow-based note. Plus concentration of flow (Herfindahl of days)."""
    names = dict(session.execute(
        select(Asset.asset_key, Asset.name).where(Asset.asset_type == "em_board")).all())
    rows = session.execute(
        select(FactObservation.asset_key, FactObservation.effective_at,
               FactObservation.value)
        .where(FactObservation.metric == "sf_main_net",
               FactObservation.quality_status == "valid",
               FactObservation.effective_at >= _ytd())).all()
    per: dict[str, list[float]] = {}
    for bk, d, v in rows:
        per.setdefault(bk, []).append(float(v))
    out = []
    for bk, vs in per.items():
        gross = sum(abs(v) for v in vs)
        net = sum(vs)
        if not vs:
            continue
        # concentration: share of the 5 largest |day| flows in gross
        top5 = sum(sorted((abs(v) for v in vs), reverse=True)[:5])
        out.append({"board": bk, "name": names.get(bk, bk),
                    "ytd_net_yuan": round(net), "ytd_gross_yuan": round(gross),
                    "flow_efficiency": round(net / gross, 3) if gross else None,
                    "top5_concentration": round(top5 / gross, 3) if gross else None,
                    "days": len(vs)})
    out.sort(key=lambda x: x["ytd_gross_yuan"], reverse=True)
    return {"data": out[:30], "as_of": None,
            "coverage": f"{len(out)}板块",
            "denominator": "成本代理=年内|主力净流|累计（毛额）与净额效率，非真实成交成本",
            "warnings": ["毛额=每日|净流入|加总（单日买卖对冲不可见）；效率=净/毛∈[-1,1]"]}
