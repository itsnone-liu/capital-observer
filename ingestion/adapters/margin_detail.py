"""沪深交易所个股两融明细（官方T+1披露）→ 申万L1行业聚合。

事实口径：仅聚合融资侧（融资余额/融资买入额，两所字段一致、单位元）；
融券侧两所口径不一致（沪市余量无余额、深市有余额），v1不聚合。
available_at 规则派生：交易所T日数据于T+1开市前发布（多年稳定的官方排程），
保守取 effective_at+1个交易日 09:15；节假日表未引入（规则已知局限）。
"""
from __future__ import annotations

import akshare as ak
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from storage.ingest import write_records  # noqa: F401  (re-exported for callers)
from storage.models import Asset, AssetMembership

SOURCE = "exchange.margin_detail_sw_l1"


def fetch_detail(date: str) -> pd.DataFrame:
    """规范化两所明细 [code, margin_balance, margin_buy]；date为ISO，接口需YYYYMMDD。"""
    ymd = date.replace("-", "")
    frames = []
    try:
        sse = ak.stock_margin_detail_sse(date=ymd)
        frames.append(pd.DataFrame({
            "code": sse["标的证券代码"].astype(str).str.zfill(6),
            "margin_balance": pd.to_numeric(sse["融资余额"]),
            "margin_buy": pd.to_numeric(sse["融资买入额"])}))
    except Exception:
        pass
    try:
        szse = ak.stock_margin_detail_szse(date=ymd)
        frames.append(pd.DataFrame({
            "code": szse["证券代码"].astype(str).str.zfill(6),
            "margin_balance": pd.to_numeric(szse["融资余额"]),
            "margin_buy": pd.to_numeric(szse["融资买入额"])}))
    except Exception:
        pass
    if not frames:
        return pd.DataFrame(columns=["code", "margin_balance", "margin_buy"])
    return pd.concat(frames, ignore_index=True)


def membership_map(session: Session, as_of: str,
                   version: str = "sw_l1_2021") -> dict[str, str]:
    """as_of当日有效的 股票→sw_l1:{industry_code} 映射（切换日属新行业）。"""
    member, group = aliased(Asset), aliased(Asset)
    rows = session.execute(
        select(member.asset_key, AssetMembership.effective_from,
               AssetMembership.effective_to, group.asset_key)
        .join(member, member.id == AssetMembership.asset_id)
        .join(group, group.id == AssetMembership.group_asset_id)
        .where(AssetMembership.classification_version == version,
               AssetMembership.effective_from <= as_of)
    ).all()
    out: dict[str, str] = {}
    for stock_key, _eff_from, eff_to, group_key in rows:
        if eff_to is not None and eff_to <= as_of:
            continue
        out[stock_key.split(":", 1)[-1]] = group_key
    return out


def industry_records(detail: pd.DataFrame, mapping: dict[str, str],
                     date: str) -> tuple[list[dict], dict]:
    """聚合到行业生成fact记录；返回(记录, 诊断{行业:成员数, unknown_count})。"""
    if detail.empty:
        return [], {"unknown_count": 0}
    mapped = detail[detail["code"].isin(mapping)].copy()
    mapped["industry"] = mapped["code"].map(mapping)
    unknown = int(len(detail) - len(mapped))
    avail = next_session_0915(date)
    records, members = [], {}
    for ind, g in mapped.groupby("industry"):
        for metric in ("margin_balance", "margin_buy"):
            records.append(dict(
                kind="fact", source_id=SOURCE, artifact_id=f"{ind}:{date}:{metric}",
                subject_key=ind, asset_key=ind, metric=metric,
                value=float(g[metric].sum()), unit="CNY", effective_at=date,
                available_at=avail, quality_status="valid"))
        members[ind] = int(len(g))
    return records, {"members": members, "unknown_count": unknown}


def next_session_0915(date: str) -> str:
    return f"{(pd.Timestamp(date) + pd.tseries.offsets.BDay(1)).date().isoformat()} 09:15:00"
