"""Active fund data adapters: NAV + disclosed industry allocation (quarterly).

Disclosure facts carry published_at = report date (disclosure lag honest);
nowcast treats them as prior at the observation time (no future leak).
"""
from __future__ import annotations

import io
from typing import Iterable

import akshare as ak
import pandas as pd

from ingestion.base import CollectionAdapter


class FundNavAdapter(CollectionAdapter):
    source_id = "em.fund_nav"
    upstream_id = "eastmoney.fundapi"
    parser_version = "v1"
    min_interval = 1.5

    def __init__(self, codes: list[str]):
        self.codes = codes

    def discover(self) -> Iterable[str]:
        return list(self.codes)

    def fetch(self, item: str) -> dict:
        df = ak.fund_open_fund_info_em(symbol=item, indicator="单位净值走势")
        buf = io.StringIO(); df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"fundapi:nav/{item}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        datecol = next((c for c in df.columns if "日期" in str(c)), None)
        navcol = next((c for c in df.columns if "单位净值" in str(c)), None)
        if not (datecol and navcol):
            return []
        recs = []
        for _, row in df.iterrows():
            if pd.notna(row[navcol]):
                recs.append(dict(kind="fact", table="fact_observation",
                                 subject_key=f"fund:{item}", asset_key=item,
                                 metric="fund_nav", value=float(row[navcol]),
                                 unit="yuan", currency="CNY",
                                 effective_at=str(row[datecol])[:10],
                                 published_at=str(row[datecol])[:10],
                                 quality_status="valid"))
        return recs


class FundIndustryAllocAdapter(CollectionAdapter):
    source_id = "em.fund_industry_alloc"
    upstream_id = "eastmoney.tiantian"
    parser_version = "v1"
    min_interval = 2.0

    def __init__(self, codes: list[str], year: str = "2025"):
        self.codes = codes
        self.year = year

    def discover(self) -> Iterable[str]:
        return list(self.codes)

    def fetch(self, item: str) -> dict:
        df = ak.fund_portfolio_industry_allocation_em(symbol=item, date=self.year)
        buf = io.StringIO(); df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"tiantian:industry_alloc/{item}/{self.year}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        recs = []
        # sample cols: 季度 | 行业 | 市值占净值比例（%）| ... (per P0: 42 rows for 4 quarters × ~10)
        qcol = next((c for c in df.columns if "季度" in str(c) or "时间" in str(c)), None)
        icol = next((c for c in df.columns if "行业" in str(c)), None)
        wcol = next((c for c in df.columns if "比例" in str(c)), None)
        if not (qcol and icol and wcol):
            return []
        # map CN industry names to SW codes via asset table happens downstream;
        # store raw-name keyed facts + let join resolve (honest about name mapping)
        for _, row in df.iterrows():
            q = str(row[qcol]).strip()
            ind = str(row[icol]).strip()
            try:
                v = float(row[wcol])
            except (TypeError, ValueError):
                continue
            # industry dimension MUST be part of the fact identity (asset_key),
            # otherwise rows overwrite each other (found in backfill audit).
            recs.append(dict(kind="fact", table="fact_observation",
                             subject_key=f"fund:{item}", asset_key=f"cnind:{ind}",
                             metric="fund_industry_weight",
                             value=v / 100.0,  # % -> fraction
                             unit="fraction", currency="CNY",
                             effective_at=q[:10] if len(q) >= 10 else q,
                             published_at=q[:10] if len(q) >= 10 else q,
                             quality_status="valid"))
        return recs
