"""Baostock index bars — FALLBACK source when sina is unreachable (P5).

Same fact schema as SinaIndexBarsAdapter (subject index:{sym}, metric
index_close/index_amount), so a fallback run seamlessly fills gaps. Login
state is per-process; adapter logs in/out per fetch batch.
"""
from __future__ import annotations

import io
from typing import Iterable

import baostock as bs
import pandas as pd

from ingestion.base import CollectionAdapter

SYMS = {"sh000300": "sh.000300", "sh000016": "sh.000016", "sh000905": "sh.000905",
        "sh000852": "sh.000852", "sh000912": "sh.000912", "sz399006": "sz.399006",
        "sh000688": "sh.000688"}


class BaoStockIndexBarsAdapter(CollectionAdapter):
    source_id = "baostock.index_daily"
    upstream_id = "baostock.com"
    parser_version = "v1"
    min_interval = 1.0

    def __init__(self, symbols: list[str] | None = None, start: str = "2023-08-01"):
        self.symbols = symbols or list(SYMS)
        self.start = start

    def discover(self) -> Iterable[str]:
        return [s for s in self.symbols if s in SYMS]

    def fetch(self, item: str) -> dict:
        code = SYMS[item]
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        try:
            rs = bs.query_history_k_data_plus(
                code, "date,close,amount", start_date=self.start,
                end_date="2099-01-01", frequency="d", adjustflag="2")
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
        finally:
            bs.logout()
        df = pd.DataFrame(rows, columns=["date", "close", "amount"])
        buf = io.StringIO(); df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"baostock:kline/{code}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        recs = []
        for _, row in df.iterrows():
            if pd.isna(row["close"]) or str(row["close"]).strip() == "":
                continue
            d = str(row["date"])
            recs.append(dict(kind="fact", table="fact_observation",
                             subject_key=f"index:{item}", asset_key=item,
                             metric="index_close", value=float(row["close"]),
                             unit="points", currency="CNY", effective_at=d,
                             published_at=d, quality_status="valid",
                             source_id=self.source_id))
            if pd.notna(row.get("amount")) and str(row["amount"]).strip():
                recs.append(dict(kind="fact", table="fact_observation",
                                 subject_key=f"index:{item}", asset_key=item,
                                 metric="index_amount", value=float(row["amount"]),
                                 unit="yuan", currency="CNY", effective_at=d,
                                 published_at=d, quality_status="valid",
                                 source_id=self.source_id))
        return recs
