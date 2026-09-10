"""ETF adapters (P0-VERIFIED channels only): sina bars, fundapi NAV,
fundf10 quarterly share change, CSI constituent weights.
"""
from __future__ import annotations

import io
from typing import Iterable

import akshare as ak
import pandas as pd

from ingestion.base import CollectionAdapter
from ingestion.probes.em_client import fund10_share_change


class ETFBarsSinaAdapter(CollectionAdapter):
    source_id = "sina.etf_bars"
    upstream_id = "sina.finance"
    parser_version = "v1"
    min_interval = 1.5

    def __init__(self, symbols: list[str], start: str = "20230801"):
        self.symbols = symbols  # e.g. ["sh510300", ...]
        self.start = start

    def discover(self) -> Iterable[str]:
        return list(self.symbols)

    def fetch(self, item: str) -> dict:
        df = ak.fund_etf_hist_sina(symbol=item)
        buf = io.StringIO(); df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"sina:fund_etf_hist_sina/{item}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        recs = []
        code = item[-6:]
        for _, row in df.iterrows():
            date = str(row["date"])
            if date < self.start:
                continue
            for metric, col, unit in (("etf_close", "close", "yuan"),
                                      ("etf_volume", "volume", "lots"),
                                      ("etf_amount", "amount", "yuan")):
                if col in row and pd.notna(row[col]):
                    recs.append(dict(kind="fact", table="fact_observation",
                                     subject_key=f"etf:{code}", asset_key=code,
                                     metric=metric, value=float(row[col]), unit=unit,
                                     currency="CNY", effective_at=date,
                                     published_at=date, quality_status="valid"))
        return recs


class ETFNavAdapter(CollectionAdapter):
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
        recs = []
        datecol = next((c for c in df.columns if "日期" in str(c)), None)
        navcol = next((c for c in df.columns if "单位净值" in str(c)), None)
        if not (datecol and navcol):
            return recs
        for _, row in df.iterrows():
            if pd.notna(row[navcol]):
                recs.append(dict(kind="fact", table="fact_observation",
                                 subject_key=f"etf:{item}", asset_key=item,
                                 metric="etf_nav", value=float(row[navcol]),
                                 unit="yuan", currency="CNY",
                                 effective_at=str(row[datecol]),
                                 published_at=str(row[datecol]),
                                 quality_status="valid"))
        return recs


class ETFShareChangeAdapter(CollectionAdapter):
    """QUARTERLY share change from fundf10 gmbd — the honest granularity."""
    source_id = "em.f10_share_change"
    upstream_id = "eastmoney.fundf10"
    parser_version = "v1"
    min_interval = 2.0

    def __init__(self, codes: list[str]):
        self.codes = codes

    def discover(self) -> Iterable[str]:
        return list(self.codes)

    def fetch(self, item: str) -> dict:
        df = fund10_share_change(item)
        buf = io.StringIO(); df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": f"fundf10:gmbd/{item}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        recs = []
        # P0/V2 sample cols: 日期 | 期间申购（亿份）| 期间赎回（亿份）|
        #                     期末总份额（亿份）| 期末净资产（亿元）| 净资产变动率
        def col(*keys: str):
            for c in df.columns:
                if all(k in str(c) for k in keys):
                    return c
            return None
        datec, subc, redc, endc, navc = (col("日期"), col("申购"), col("赎回"),
                                         col("期末", "份额"), col("期末", "净资产"))
        if datec is None:
            return recs

        def emit(c, metric, factor, unit):
            for _, row in df.iterrows():
                v = row.get(c) if c else None
                if v is None or pd.isna(v) or str(v).strip() in ("---", "-", ""):
                    continue  # honest skip: missing quarter stays missing
                try:
                    val = float(str(v).replace(",", "")) * factor
                except ValueError:
                    continue
                d = str(row[datec]).strip()[:10]
                if len(d) != 10:
                    continue
                recs.append(dict(kind="fact", table="fact_observation",
                                 subject_key=f"etf:{item}", asset_key=item,
                                 metric=metric, value=val, unit=unit,
                                 currency="CNY", effective_at=d,
                                 published_at=d, quality_status="valid"))

        emit(endc, "etf_share_end", 1e8, "shares")
        emit(subc, "etf_share_subscribe", 1e8, "shares")
        emit(redc, "etf_share_redeem", 1e8, "shares")
        emit(navc, "etf_net_asset", 1e8, "yuan")
        return recs


class CSIWeightAdapter(CollectionAdapter):
    """CSI index constituents+weights snapshot; monthly archive builds history."""
    source_id = "csindex.cons_weight"
    upstream_id = "csindex.com.cn"
    parser_version = "v1"
    min_interval = 3.0

    def __init__(self, index_codes: list[str]):
        self.index_codes = index_codes

    def discover(self) -> Iterable[str]:
        return list(self.index_codes)

    def fetch(self, item: str) -> dict:
        cons = ak.index_stock_cons_weight_csindex(symbol=item)
        buf = io.StringIO(); cons.to_csv(buf, index=False)
        import datetime as dt
        asof = dt.date.today().strftime("%Y%m%d")
        return {"payload": buf.getvalue(),
                "url": f"csindex:cons_weight/{item}@{asof}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        import datetime as dt
        asof = dt.date.today().isoformat()
        # weight rows -> asset_membership records are handled by a dedicated
        # loader; here we emit membership facts for provenance
        code_col = next((c for c in df.columns if "成分券代码" in str(c)), None)
        wcol = next((c for c in df.columns if "权重" in str(c) or "加权比例" in str(c)), None)
        if code_col is None:
            return []
        recs = []
        for _, row in df.iterrows():
            w = row.get(wcol) if wcol else None
            recs.append(dict(kind="membership", table="asset_membership",
                             group_key=item, asset_key=str(row[code_col]).zfill(6),
                             weight=float(w) if pd.notna(w) else None,
                             classification_version=f"csi_{asof}",
                             effective_from=asof,
                             source_id=self.source_id))
        return recs
