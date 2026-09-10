"""Ingest TDX board-index daily bars (880xxx) from local vipdoc into facts.

Reads .day files under /root/tdx_data/vipdoc/sh/lday, filtered to the boards
in configs/tdx_group_index.json. Stores close + amount (成交额, 元) with
subject tdx880:<code> so the panel API can join full-year price/turnover
context (covers Jan-Mar where EM flow history is unavailable).
"""
import json, struct, sys, datetime as dt
from pathlib import Path

sys.path.insert(0, "/root/project/workspace/capital-observer")
from sqlalchemy import select
from sqlalchemy.orm import Session
from storage.models import Asset
from storage.ingest import write_records
from jobs.backfill import DB_URL
from sqlalchemy import create_engine

VIPDOC = Path("/root/tdx_data/vipdoc/sh/lday")
ROOT = Path("/root/project/workspace/capital-observer")


def read_day(path: Path):
    out = []
    data = path.read_bytes()
    for i in range(0, len(data) - 31, 32):
        b = data[i:i + 32]
        d = int.from_bytes(b[0:4], "little")
        o = int.from_bytes(b[4:8], "little") / 100
        h = int.from_bytes(b[8:12], "little") / 100
        l = int.from_bytes(b[12:16], "little") / 100
        c = int.from_bytes(b[16:20], "little") / 100
        amount = struct.unpack("<f", b[20:24])[0]      # 成交额(元)
        vol = int.from_bytes(b[24:28], "little")        # 成交量(股)
        if d < 20260101:
            continue
        out.append((str(dt.date(d // 10000, d // 100 % 100, d % 100)), c, amount, vol))
    return out


def main():
    groups = json.loads((ROOT / "configs" / "tdx_group_index.json").read_text())
    codes = sorted({c for v in groups.values() for c in v})
    engine = create_engine(DB_URL, connect_args={"timeout": 60})
    today = dt.date.today().isoformat()
    total = 0
    with Session(engine) as s:
        for code in codes:
            f = VIPDOC / f"sh{code}.day"
            if not f.exists():
                print(f"{code}: file missing")
                continue
            recs = []
            for d, c, amount, vol in read_day(f):
                for metric, val in (("tdx_close", c), ("tdx_amount", amount)):
                    recs.append(dict(kind="fact", subject_key=f"tdx880:{code}",
                                     metric=metric, value=val, unit="yuan" if metric == "tdx_amount" else "point",
                                     effective_at=d, published_at=d,
                                     source_id="local.tdx_day", artifact_id=f"vipdoc:sh{code}",
                                     quality_status="valid",
                                     basis="TDX板块指数日线(本地vipdoc)", scenario="FACT"))
            _ensure(s, code)
            st = write_records(s, recs, fill_only=False)
            s.commit()
            total += len(recs)
            print(f"{code}: {len(recs)} facts {st.get('fact_inserted', 0)}ins/{st.get('skipped_unchanged', 0)}skip")
    print("total", total)


def _ensure(s, code):
    a = s.execute(select(Asset).where(Asset.asset_key == f"tdx880:{code}")).scalar_one_or_none()
    if a is None:
        s.add(Asset(asset_key=f"tdx880:{code}", asset_type="index", name=f"TDX板块{code}"))
        s.commit()


if __name__ == "__main__":
    main()
