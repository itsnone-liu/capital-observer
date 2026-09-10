"""Shenwan (SW) L1 industry index adapter (P0-VERIFIED, full history 1999+).

Sector ranking backbone (design sec.11 sectors/ranking): 31 industries,
daily bars + valuation snapshot from the same upstream.
Facts are public market data -> published_at = trading date.
"""
from __future__ import annotations

import io
from typing import Iterable

import akshare as ak
import pandas as pd

from ingestion.base import CollectionAdapter


class SWIndustryBarsAdapter(CollectionAdapter):
    source_id = "sw.industry_daily"
    upstream_id = "swsresearch.com"
    parser_version = "v1"
    min_interval = 2.0

    def __init__(self, industries: list[str] | None = None, start: str | None = None):
        self.industries = industries  # e.g. ["801080"]; None = all L1
        self.start = start

    def discover(self) -> Iterable[str]:
        if self.industries:
            return list(self.industries)
        df = ak.sw_index_first_info()
        return [str(c).split(".")[0] for c in df["行业代码"].tolist()]

    def fetch(self, item: str) -> dict:
        df = ak.index_hist_sw(symbol=item, period="day")
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"swsresearch:index_hist_sw/{item}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        recs = []
        for _, row in df.iterrows():
            date = str(row["日期"])
            if self.start and date < self.start:
                continue
            if pd.notna(row.get("收盘")):
                recs.append(dict(kind="fact", table="fact_observation",
                                 subject_key=f"industry:{item}", asset_key=item,
                                 metric="sw_close", value=float(row["收盘"]),
                                 unit="points", currency="CNY",
                                 effective_at=date, published_at=date,
                                 quality_status="valid"))
            if pd.notna(row.get("成交额")):
                recs.append(dict(kind="fact", table="fact_observation",
                                 subject_key=f"industry:{item}", asset_key=item,
                                 metric="sw_amount", value=float(row["成交额"]),
                                 unit="yuan_100m", currency="CNY",
                                 effective_at=date, published_at=date,
                                 quality_status="valid"))
        return recs

    def validate(self, records: list[dict]) -> tuple[list[dict], list[str]]:
        good, warns = super().validate(records)
        dates = {r["effective_at"] for r in good}
        if len(dates) < 100:
            warns.append(f"{good[0]['subject_key'] if good else '?'}: only {len(dates)} days")
        return good, warns


class SWIndustryListAdapter(CollectionAdapter):
    """Industry registry + valuation snapshot (静态PE/PB/股息率 — context layer)."""
    source_id = "sw.industry_list"
    upstream_id = "swsresearch.com"
    parser_version = "v1"
    min_interval = 2.0

    def discover(self) -> Iterable[str]:
        return ["latest"]

    def fetch(self, item: str) -> dict:
        df = ak.sw_index_first_info()
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": "swsresearch:sw_index_first_info",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        import datetime as dt
        asof = dt.date.today().isoformat()
        recs = []
        for _, row in df.iterrows():
            code = str(row["行业代码"]).split(".")[0]
            name = str(row["行业名称"])
            recs.append(dict(kind="asset_info", table="asset", asset_key=code,
                             asset_type="industry", name=name))
            for metric, col, unit in (("sw_member_count", "成份个数", "count"),
                                      ("sw_pe_static", "静态市盈率", "ratio"),
                                      ("sw_pb", "市净率", "ratio"),
                                      ("sw_div_yield", "静态股息率", "pct")):
                if pd.notna(row.get(col)):
                    recs.append(dict(kind="fact", table="fact_observation",
                                     subject_key=f"industry:{code}", asset_key=code,
                                     metric=metric, value=float(row[col]),
                                     unit=unit, currency="CNY",
                                     effective_at=asof, published_at=asof,
                                     quality_status="valid"))
        return recs
