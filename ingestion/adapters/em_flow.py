"""EM sector fund-flow adapters (push2delay/push2his — VERIFIED reachable).

Three datasets:
  1. EMSectorFlowSnapshotAdapter — today's flow ranking, all ~496 boards
     (clist fs=m:90+t:2): main/super-large/large/medium/small net inflow (yuan)
  2. EMSectorFlowHistoryAdapter — per-board daily flow history (fflow daykline)
  3. EMStockIndustryAdapter — whole-A stock->EM board mapping via clist f100

Caveat printed downstream: "主力" = large-order bucket (exchange-L1 auction
size proxy), an industry-convention proxy for institutional activity, NOT
identified institutional holdings.
"""
from __future__ import annotations

import io
import json
import time
from typing import Iterable

import pandas as pd
import requests

from ingestion.base import CollectionAdapter

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
REFERER = "https://quote.eastmoney.com/"


def _get(url: str, params: dict, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=12,
                             headers={"User-Agent": UA, "Referer": REFERER})
            if r.status_code == 200 and r.text.strip():
                return r.json()
        except requests.RequestException as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"EM flow fetch failed after {tries}: {last}")


class EMSectorFlowSnapshotAdapter(CollectionAdapter):
    source_id = "em.sector_flow_snapshot"
    upstream_id = "eastmoney.push2delay"
    parser_version = "v1"
    min_interval = 1.2

    def discover(self) -> Iterable[str]:
        return ["latest"]

    def fetch(self, item: str) -> dict:
        rows, pn = [], 1
        while True:
            js = _get("https://push2delay.eastmoney.com/api/qt/clist/get", {
                "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f62", "fs": "m:90+t:2",
                "fields": "f12,f14,f3,f62,f66,f72,f78,f84,f184,f204,f205"})
            data = js.get("data") or {}
            diff = data.get("diff") or []
            rows.extend(diff)
            if len(rows) >= (data.get("total") or 0) or not diff:
                break
            pn += 1
            time.sleep(self.min_interval)
        return {"payload": json.dumps(rows), "url": "push2delay:clist/sector_flow",
                "content_type": "application/json"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        rows = json.loads(raw["payload"])
        import datetime as dt
        today = dt.date.today().isoformat()
        recs = []
        for x in rows:
            bk = str(x.get("f12", ""))
            if not bk.startswith("BK"):
                continue
            # '-' means no quote (suspended board): skip honestly
            def num(v):
                try:
                    return None if v in ("-", None, "") else float(v)
                except (TypeError, ValueError):
                    return None
            for metric, fld, unit in (
                    ("sf_main_net", "f62", "yuan"),          # 主力净流入
                    ("sf_superlarge_net", "f66", "yuan"),    # 超大单
                    ("sf_large_net", "f72", "yuan"),         # 大单
                    ("sf_medium_net", "f78", "yuan"),        # 中单
                    ("sf_small_net", "f84", "yuan"),         # 小单
                    ("sf_main_net_pct", "f184", "pct")):     # 主力净占比
                v = num(x.get(fld))
                if v is None:
                    continue
                recs.append(dict(kind="fact", table="fact_observation",
                                 subject_key=f"bk:{bk}", asset_key=bk,
                                 metric=metric, value=v, unit=unit,
                                 currency="CNY", effective_at=today,
                                 published_at=today, quality_status="valid",
                                 extra_name=x.get("f14")))
            # board name as asset_info (asset table)
            recs.append(dict(kind="asset_info", table="asset", asset_key=bk,
                             asset_type="em_board", name=str(x.get("f14", ""))))
        return recs


class EMSectorFlowHistoryAdapter(CollectionAdapter):
    source_id = "em.sector_flow_history"
    upstream_id = "eastmoney.push2his"
    parser_version = "v1"
    min_interval = 1.2

    def __init__(self, boards: list[str], lmt: int = 120):
        self.boards = boards  # BK codes
        self.lmt = lmt

    def discover(self) -> Iterable[str]:
        return list(self.boards)

    def fetch(self, item: str) -> dict:
        js = _get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get", {
            "secid": f"90.{item}", "lmt": self.lmt, "klt": "101",
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f61,f62"})
        kl = ((js.get("data") or {}).get("klines")) or []
        return {"payload": "\n".join(kl), "url": f"push2his:fflow/{item}",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        recs = []
        # fields2 order: date, main, small, medium, large, superlarge, pct_chg, close
        cols = {"date": 0, "main": 1, "small": 2, "medium": 3,
                "large": 4, "superlarge": 5, "pct_chg": 6}
        for line in raw["payload"].splitlines():
            parts = line.split(",")
            if len(parts) < 7:
                continue
            d = parts[cols["date"]]
            for metric, idx, unit in (("sf_main_net", "main", "yuan"),
                                      ("sf_small_net", "small", "yuan"),
                                      ("sf_medium_net", "medium", "yuan"),
                                      ("sf_large_net", "large", "yuan"),
                                      ("sf_superlarge_net", "superlarge", "yuan")):
                try:
                    v = float(parts[cols[idx]])
                except ValueError:
                    continue
                recs.append(dict(kind="fact", table="fact_observation",
                                 subject_key=f"bk:{item}", asset_key=item,
                                 metric=metric, value=v, unit=unit,
                                 currency="CNY", effective_at=d,
                                 published_at=d, quality_status="valid"))
        return recs


class EMStockIndustryAdapter(CollectionAdapter):
    """Whole-A stock -> EM industry board mapping (clist f100)."""
    source_id = "em.stock_industry_map"
    upstream_id = "eastmoney.push2delay"
    parser_version = "v1"
    min_interval = 1.2

    def discover(self) -> Iterable[str]:
        return ["all"]

    def fetch(self, item: str) -> dict:
        rows, pn = [], 1
        while True:
            js = _get("https://push2delay.eastmoney.com/api/qt/clist/get", {
                "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f12",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                "fields": "f12,f14,f100"})
            data = js.get("data") or {}
            diff = data.get("diff") or []
            rows.extend(diff)
            if len(rows) >= (data.get("total") or 0) or not diff or pn > 60:
                break
            pn += 1
            time.sleep(self.min_interval)
        df = pd.DataFrame(rows)
        buf = io.StringIO(); df.to_csv(buf, index=False)
        return {"payload": buf.getvalue(), "url": "push2delay:clist/stock_industry",
                "content_type": "text/csv"}

    def parse(self, item: str, raw: dict) -> list[dict]:
        df = pd.read_csv(io.StringIO(raw["payload"]))
        import datetime as dt
        today = dt.date.today().isoformat()
        recs = []
        for _, row in df.iterrows():
            stock = str(row.get("f12", "")).zfill(6)
            ind = str(row.get("f100", "")).strip()
            if not stock.isdigit() or not ind:
                continue
            recs.append(dict(kind="membership", table="asset_membership",
                             group_key=f"em_board:{ind}", asset_key=f"stock:{stock}",
                             effective_from=today, classification_version="em_industry_v1",
                             weight=None))
        return recs
