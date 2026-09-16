"""ETF 每日总份额 adapters — 交易所官方直连（2026-09-15 P0 复验通过）。

sse: 逐日快照（任意历史日期；当日数据 T 晚清算后才发布，未出→空）
szse: 官方 xlsx 区间查询（单次≤6个月，深史可回补，分段抓取）

份额是交易所清算事实：metric=etf_total_shares unit=shares。
Δ份额与 Δ×NAV 的资金流估算在读取侧（API/normalization）派生，
不作为 fact 落库（sec.4: 估算不进 fact 表）。
跨源不做差分（两所清算时点不同，跨源 Δ 是假流量）。
"""
from __future__ import annotations

import io
import time
from datetime import date, timedelta
from typing import Iterable

import akshare as ak
import pandas as pd

from ingestion.base import CollectionAdapter

METRIC = "etf_total_shares"
SZSE_CHUNK_DAYS = 150  # < 6个月/次（文档限制），留安全余量
_CHUNK_SEP = "\x00"    # CSV 内含换行，分块不能用 \n 拼接


def _dstr(d) -> str:
    return d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d)


def trading_days_since(start: str, end: str) -> list[str]:
    """粗交易日列表（去周末；节假日当日源侧自然返回空，由增量跳过）。"""
    s = pd.Timestamp(start).date()
    e = pd.Timestamp(end).date()
    days = []
    while s <= e:
        if s.weekday() < 5:
            days.append(s.strftime("%Y%m%d"))
        s += timedelta(days=1)
    return days


class SSEETFShareAdapter(CollectionAdapter):
    """上交所 ETF 逐日份额快照（akshare fund_etf_scale_sse）。"""

    source_id = "sse.etf_share"
    upstream_id = "sse.com.cn"
    parser_version = "v1"
    min_interval = 1.5

    def __init__(self, dates: list[str]):
        self.dates = [d.replace("-", "") for d in dates]  # YYYYMMDD

    def discover(self) -> Iterable[str]:
        return list(self.dates)

    def fetch(self, item: str) -> dict:
        self.throttle()
        try:
            df = ak.fund_etf_scale_sse(date=item)
        except (KeyError, ValueError):
            # 当日未清算 / 非交易日：返回空载荷，parse 出 0 条
            return {"payload": "", "url": f"sse:fund_etf_scale_sse/{item}",
                    "content_type": "text/csv"}
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"sse:fund_etf_scale_sse/{item}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        if not raw["payload"]:
            return []
        df = pd.read_csv(io.StringIO(raw["payload"]), dtype={"基金代码": str})
        date_iso = f"{item[:4]}-{item[4:6]}-{item[6:]}"
        recs = []
        for _, row in df.iterrows():
            code = str(row.get("基金代码", "")).zfill(6)
            shares = row.get("基金份额")
            if not code.isdigit() or pd.isna(shares):
                continue
            recs.append(dict(kind="fact", table="fact_observation",
                             subject_key=f"etf:{code}", asset_key=code,
                             metric=METRIC, value=float(shares), unit="shares",
                             currency="CNY", effective_at=date_iso,
                             # 交易所官方日度文件，T+1清算后发布；发布时刻以
                             # 实际首次抓到的时间计（JobRunner回填fetched_at）。
                             published_at=None, published_at_verified=True,
                             quality_status="valid"))
        return recs


class SZSEETFShareAdapter(CollectionAdapter):
    """深交所 ETF 份额区间（官方 xlsx '基金规模(份)'，≤6个月/段）。"""

    source_id = "szse.etf_share"
    upstream_id = "szse.cn"
    parser_version = "v1"
    min_interval = 2.5

    def __init__(self, start: str, end: str, retries: int = 3):
        self.start = start.replace("-", "")
        self.end = end.replace("-", "")
        self.retries = retries

    def discover(self) -> Iterable[str]:
        return [f"{self.start}-{self.end}"]

    def _chunks(self) -> list[tuple[str, str]]:
        s = pd.Timestamp(self.start).date()
        e = pd.Timestamp(self.end).date()
        out = []
        while s <= e:
            ce = min(s + timedelta(days=SZSE_CHUNK_DAYS), e)
            out.append((s.strftime("%Y%m%d"), ce.strftime("%Y%m%d")))
            s = ce + timedelta(days=1)
        return out

    def fetch(self, item: str) -> dict:
        parts = []
        for cs, ce in self._chunks():
            last_exc: Exception | None = None
            for attempt in range(self.retries):  # szse 偶发 RST，重试即过
                self.throttle()
                try:
                    df = ak.fund_scale_daily_szse(start_date=cs, end_date=ce)
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 — 记录后重试
                    last_exc = exc
                    time.sleep(2.0 * (attempt + 1))
            if last_exc is not None:
                continue
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            parts.append(buf.getvalue())
        return {"payload": _CHUNK_SEP.join(parts), "url": "szse:fund_scale_daily_szse/"
                f"{self.start}-{self.end}", "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        if not raw["payload"]:
            return []
        frames = []
        for chunk in raw["payload"].split(_CHUNK_SEP):
            if chunk.strip():
                frames.append(pd.read_csv(io.StringIO(chunk), dtype={"基金代码": str}))
        if not frames:
            return []
        df = (pd.concat(frames, ignore_index=True)
                .drop_duplicates(subset=["日期", "基金代码"]))
        recs = []
        for _, row in df.iterrows():
            code = str(row.get("基金代码", "")).zfill(6)
            shares = row.get("基金份额")
            d = str(row.get("日期", "")).replace("-", "")
            if not code.isdigit() or pd.isna(shares) or len(d) != 8:
                continue
            recs.append(dict(kind="fact", table="fact_observation",
                             subject_key=f"etf:{code}", asset_key=code,
                             metric=METRIC, value=float(shares), unit="shares",
                             currency="CNY", effective_at=f"{d[:4]}-{d[4:6]}-{d[6:]}",
                             published_at=None, published_at_verified=True,
                             quality_status="valid"))
        return recs


def latest_share_dates(engine) -> dict[str, str]:
    """各源已入库的最新份额日期（增量起点）。"""
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text(
            "select source_id, max(effective_at) from fact_observation "
            "where metric = :m group by source_id"), {"m": METRIC}).all()
    return {r[0]: r[1] for r in rows if r[1]}
