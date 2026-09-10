"""Sina index daily bars adapter (P0-VERIFIED upstream, full history).

First production adapter: exercises the whole sec.4 contract —
discover/fetch/parse/validate + artifact archive + job_run bookkeeping.
Bars are FACTS (public market data): published_at = trading date close.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import akshare as ak

from ingestion.base import CollectionAdapter

#: index universe (P0 first batch, design sec.1). Sina symbols.
INDEX_UNIVERSE = {
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "sh000016": "上证50",
    "sh000688": "科创50",
    "sz399006": "创业板指",
    "sh000912": "中证2000",
}

CURRENCY = "CNY"


class SinaIndexBarsAdapter(CollectionAdapter):
    source_id = "sina.index_daily"
    upstream_id = "sina.finance"
    parser_version = "v1"
    min_interval = 1.5

    def __init__(self, symbols: list[str] | None = None, start: str | None = None):
        self.symbols = symbols or list(INDEX_UNIVERSE)
        self.start = start  # optional ISO date filter (inclusive)

    def discover(self) -> Iterable[str]:
        return list(self.symbols)

    def fetch(self, item: str) -> dict:
        df = ak.stock_zh_index_daily(symbol=item)  # full history, ~1.5s per symbol
        import io
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"sina:stock_zh_index_daily/{item}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        import io
        import pandas as pd
        df = pd.read_csv(io.StringIO(raw["payload"]))
        recs = []
        for _, row in df.iterrows():
            date = str(row["date"])
            if self.start and date < self.start:
                continue
            # volume on sina index is in 手(100 shares) for stocks; index level = points.
            for metric, col, unit in (("close", "close", "points"),
                                      ("volume", "volume", "lots"),
                                      ("amount", "amount", "yuan")):
                if col in row and pd.notna(row[col]):
                    recs.append({
                        "kind": "fact",
                        "table": "fact_observation",
                        "subject_key": f"index:{item}",
                        "asset_key": item,
                        "metric": f"index_{metric}",
                        "value": float(row[col]),
                        "unit": unit,
                        "currency": CURRENCY,
                        "effective_at": date,
                        "published_at": date,  # public market data: same-day close
                        "quality_status": "valid",
                    })
        return recs

    def validate(self, records: list[dict]) -> tuple[list[dict], list[str]]:
        good, warns = super().validate(records)
        # monotonic dates per subject; detect gaps > 15 calendar days (holidays ok)
        by_date: dict[str, set] = {}
        for r in good:
            by_date.setdefault(r["subject_key"], set()).add(r["effective_at"])
        import datetime as dt
        for subj, dates in by_date.items():
            ds = sorted(dt.date.fromisoformat(d) for d in dates)
            if len(ds) >= 2:
                maxgap = max((b - a).days for a, b in zip(ds, ds[1:]))
                if maxgap > 15:
                    warns.append(f"{subj}: max calendar gap {maxgap}d (holiday or missing)")
        return good, warns
