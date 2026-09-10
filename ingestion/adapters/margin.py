"""Margin (两融) adapters — exchange-direct, P0-VERIFIED.

sse: range summary (market-level daily, multi-year direct)
szse: per-day summary loop
detail: per-underlying per-day (industry concentration input)

Facts are exchange-published after close: published_at = trade date +delay note.
Ratios vs free-float mcap computed downstream (never stored as fact here).
"""
from __future__ import annotations

import io
from typing import Iterable

import akshare as ak
import pandas as pd

from ingestion.base import CollectionAdapter


def _dstr(d) -> str:
    return d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d)


class SSEMarginSummaryAdapter(CollectionAdapter):
    source_id = "sse.margin_summary"
    upstream_id = "sse.com.cn"
    parser_version = "v1"
    min_interval = 2.0

    def __init__(self, start: str = "20230801", end: str | None = None):
        self.start = start
        self.end = end

    def discover(self) -> Iterable[str]:
        return [f"{self.start}-{self.end or 'today'}"]

    def fetch(self, item: str) -> dict:
        s, e = item.split("-")
        e = None if e == "today" else e
        df = ak.stock_margin_sse(start_date=s, end_date=e)
        buf = io.StringIO(); df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": "sse:stock_margin_sse",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        datecol = [c for c in df.columns if "信用交易日期" in str(c) or "日期" in str(c)][0]
        # EXACT column mapping — prefix matching caused 融券余量金额 to overwrite
        # 融券余量 with a yuan value (found in backfill audit, 2026-09-10).
        exact = {"融资余额": "margin_fin_balance", "融资买入额": "margin_fin_buy",
                 "融券余量": "margin_sec_volume", "融券余量金额": "margin_sec_balance",
                 "融券卖出量": "margin_sec_sell", "融资融券余额": "margin_total_balance"}
        mapping = {c: exact[str(c).strip()] for c in df.columns if str(c).strip() in exact}
        recs = []
        for _, row in df.iterrows():
            date = str(row[datecol]).replace("-", "")[:8]
            if len(date) != 8 or not date.isdigit():
                continue
            for col, metric in mapping.items():
                v = row.get(col)
                if pd.notna(v):
                    unit = "shares" if metric == "margin_sec_volume" else "yuan"
                    recs.append(dict(kind="fact", table="fact_observation",
                                     subject_key="market:SSE", asset_key=None,
                                     metric=metric, value=float(v), unit=unit,
                                     currency="CNY", effective_at=date,
                                     published_at=date, quality_status="valid"))
        return recs


class SZSEMarginSummaryAdapter(CollectionAdapter):
    """SZSE market margin summary — per-day API, values in 亿元."""
    source_id = "szse.margin_summary"
    upstream_id = "szse.cn"
    parser_version = "v1"
    min_interval = 2.0

    def __init__(self, dates: list[str]):
        self.dates = dates  # YYYYMMDD

    def discover(self) -> Iterable[str]:
        return list(self.dates)

    def fetch(self, item: str) -> dict:
        df = ak.stock_margin_szse(date=item)
        buf = io.StringIO(); df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"szse:stock_margin_szse/{item}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        if df.empty or df.isna().all().all():
            return []  # not yet published for this date — honest empty, retry at cron
        row = df.iloc[0]
        mapping = {"融资余额": "margin_fin_balance", "融资买入额": "margin_fin_buy",
                   "融券余量": "margin_sec_volume", "融券余额": "margin_sec_balance",
                   "融券卖出量": "margin_sec_sell", "融资融券余额": "margin_total_balance"}
        recs = []
        for col, metric in mapping.items():
            v = row.get(col)
            if pd.notna(v):
                unit = "shares" if metric == "margin_sec_volume" else "yuan"
                val = float(v) * (1e4 if unit == "shares" else 1e8)  # 亿元/万份 -> yuan/shares
                recs.append(dict(kind="fact", table="fact_observation",
                                 subject_key="market:SZSE", asset_key=None,
                                 metric=metric, value=val, unit=unit,
                                 currency="CNY", effective_at=item,
                                 published_at=item, quality_status="valid"))
        return recs


class MarginDetailDayAdapter(CollectionAdapter):
    """Per-underlying margin detail for ONE day, both exchanges.

    discover() yields trade dates; industry aggregation happens downstream
    by joining asset_membership — never summed here.
    """
    source_id = "exch.margin_detail"
    upstream_id = "sse.com.cn+szse.cn"
    parser_version = "v1"
    min_interval = 2.0

    def __init__(self, dates: list[str]):
        self.dates = dates  # YYYYMMDD

    def discover(self) -> Iterable[str]:
        return list(self.dates)

    def fetch(self, item: str) -> dict:
        frames = []
        for fn, exch in ((ak.stock_margin_detail_sse, "SSE"),
                         (ak.stock_margin_detail_szse, "SZSE")):
            try:
                df = fn(date=item)
                df["__exch"] = exch
                frames.append(df)
            except Exception:
                continue  # one exchange missing -> partial, recorded by runner
        import time; time.sleep(1.5)
        if not frames:
            raise RuntimeError(f"no detail data for {item}")
        df = pd.concat(frames, ignore_index=True)
        buf = io.StringIO(); df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"exch:margin_detail/{item}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        recs = []
        code_col = next((c for c in df.columns if "代码" in str(c) or "标的" in str(c)), None)
        fin_cols = {"融资余额": "margin_fin_balance", "融券余量": "margin_sec_volume",
                    "融资买入额": "margin_fin_buy", "融资偿还额": "margin_fin_repay"}
        if code_col is None:
            return recs
        for _, row in df.iterrows():
            code = str(row[code_col]).zfill(6)[:6]
            for c, metric in fin_cols.items():
                v = row.get(c)
                if pd.notna(v):
                    recs.append(dict(kind="fact", table="fact_observation",
                                     subject_key=f"stock:{code}", asset_key=code,
                                     metric=metric, value=float(v),
                                     unit="yuan" if "fin" in metric else "shares",
                                     currency="CNY", effective_at=item,
                                     published_at=item, quality_status="valid"))
        return recs
