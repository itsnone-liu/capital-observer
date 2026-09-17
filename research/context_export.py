"""PIT 背景批量导出（研究消费端契约）。

与 /api/v1/observations 相同的 strict-PIT 过滤（available_at 非空且 <= as_of、
quality_status=valid），但一次性导出多指标长表；成员导出为全量生效区间，
供研究侧自行做有效期关联与重叠检测。本模块不回填、不估算、不猜缺失行业。

边界：ETF 份额×净值 = 新增 ETF 暴露规模（申赎口径），不是二级市场现金买入；
导出仅做同日成对相乘，缺任一即无行，不做跨日插值。
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased, Session

from storage.models import Asset, AssetMembership, FactObservation

# 研究侧默认消费的背景指标（可被 CLI 覆盖）。
# 可用性口径：index/etf/sw 价格量额=T日15:30、份额净值=T日22:00
# （scripts/stamp_background_availability.py 保守回补，2026-09-17裁定）；
# margin_fin_* 交易所明细仍在范围外，strict-PIT 导出为空是诚实行为。
DEFAULT_METRICS = ("index_close", "etf_total_shares", "etf_nav",
                   "margin_fin_balance", "margin_balance",
                   "sw_close", "sw_amount", "sf_main_net")


def fact_coverage(session: Session, metrics=DEFAULT_METRICS, *,
                  start: str | None = None, end: str | None = None,
                  as_of: str | None = None) -> pd.DataFrame:
    """逐指标披露：区间内行数 / 带可用性戳行数 / 在 as_of 前可用行数。

    缺口必须可见：无戳历史不能被 strict-PIT 导出，也不能被静默当成零。
    """
    conds = [FactObservation.metric.in_(tuple(metrics))]
    if start is not None:
        conds.append(FactObservation.effective_at >= start)
    if end is not None:
        conds.append(FactObservation.effective_at <= end)
    pit_cond = FactObservation.available_at.is_not(None)
    if as_of is not None:
        pit_cond = pit_cond & (FactObservation.available_at <= as_of)
    rows = session.execute(
        select(FactObservation.metric,
               func.count().label("rows_in_range"),
               func.count(FactObservation.available_at).label("rows_with_availability"),
               func.sum(case((pit_cond, 1), else_=0)).label("pit_usable"))
        .where(*conds).group_by(FactObservation.metric)
        .order_by(FactObservation.metric)
    ).all()
    return pd.DataFrame(rows, columns=["metric", "rows_in_range",
                                       "rows_with_availability", "pit_usable"])


def export_facts(session: Session, metrics=DEFAULT_METRICS, *,
                 start: str | None = None, end: str | None = None,
                 as_of: str | None = None) -> pd.DataFrame:
    """strict-PIT 事实长表：available_at 未知或晚于 as_of 的一律排除。"""
    q = select(FactObservation).where(
        FactObservation.metric.in_(tuple(metrics)),
        FactObservation.available_at.is_not(None),
        FactObservation.quality_status == "valid",
    )
    if as_of is not None:
        q = q.where(FactObservation.available_at <= as_of)
    if start is not None:
        q = q.where(FactObservation.effective_at >= start)
    if end is not None:
        q = q.where(FactObservation.effective_at <= end)
    rows = session.execute(q.order_by(FactObservation.effective_at,
                                      FactObservation.subject_key)).scalars().all()
    return pd.DataFrame([{
        "subject_key": r.subject_key, "asset_key": r.asset_key,
        "metric": r.metric, "value": r.value, "unit": r.unit,
        "effective_at": r.effective_at, "available_at": r.available_at,
    } for r in rows], columns=["subject_key", "asset_key", "metric", "value",
                               "unit", "effective_at", "available_at"])


def etf_new_exposure(facts: pd.DataFrame, *,
                     share_metric: str = "etf_total_shares",
                     nav_metric: str = "etf_nav") -> pd.DataFrame:
    """同日 share×nav 成对相乘 = 新增 ETF 暴露规模；不成对即缺行。"""
    cols = ["subject_key", "effective_at", "share", "nav", "new_exposure_yuan"]
    if facts.empty:
        return pd.DataFrame(columns=cols)
    shares = (facts[facts["metric"] == share_metric]
              [["subject_key", "effective_at", "value"]].rename(columns={"value": "share"}))
    navs = (facts[facts["metric"] == nav_metric]
            [["subject_key", "effective_at", "value"]].rename(columns={"value": "nav"}))
    m = shares.merge(navs, on=["subject_key", "effective_at"], how="inner")
    m["new_exposure_yuan"] = m["share"] * m["nav"]
    return (m[cols].sort_values(["effective_at", "subject_key"])
            .reset_index(drop=True))


def export_membership_history(session: Session,
                              version: str = "sw_l1_2021") -> pd.DataFrame:
    """全量生效区间成员表（code, industry_code, industry_name, from, to）。

    to 为空 = 至今有效；研究侧 join 时必须自检同一 code 区间不重叠。
    """
    member, group = aliased(Asset), aliased(Asset)
    rows = session.execute(
        select(member.asset_key, AssetMembership.effective_from,
               AssetMembership.effective_to, group.asset_key, group.name)
        .join(member, member.id == AssetMembership.asset_id)
        .join(group, group.id == AssetMembership.group_asset_id)
        .where(AssetMembership.classification_version == version)
        .order_by(member.asset_key, AssetMembership.effective_from)
    ).all()
    return pd.DataFrame([{
        "code": k.split(":", 1)[1] if ":" in k else k,
        "industry_code": g.split(":", 1)[-1],
        "industry_name": n,
        "effective_from": f, "effective_to": t,
    } for k, f, t, g, n in rows],
        columns=["code", "industry_code", "industry_name",
                 "effective_from", "effective_to"])
