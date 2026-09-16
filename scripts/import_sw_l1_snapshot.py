#!/usr/bin/env python3
"""导入申万一级行业成分快照（akshare index_component_sw，官方计入日期）。

- classification_version=sw_l1_2021；effective_from=官方计入日期；
- 窗口内计入但早于计入日已上市的=真换组：其换组前旧行业未知，不落错误行，
  查询端点对早于计入日的日期返回 no_membership（=unknown）；
- 幂等：重复导入只补新行；复用 write_records，换组时自动闭旧区间。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import akshare as ak
import pandas as pd
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.ingest import write_records  # noqa: E402
from storage.models import Base, AssetMembership  # noqa: E402
from storage.models import init_db  # noqa: E402

SW_L1 = {"801010":"农林牧渔","801030":"基础化工","801040":"钢铁","801050":"有色金属",
"801080":"电子","801880":"汽车","801110":"家用电器","801120":"食品饮料","801130":"纺织服饰",
"801140":"轻工制造","801150":"医药生物","801160":"公用事业","801170":"交通运输",
"801180":"房地产","801200":"商贸零售","801210":"社会服务","801230":"综合","801710":"建筑材料",
"801720":"建筑装饰","801730":"电力设备","801740":"国防军工","801750":"计算机","801760":"传媒",
"801770":"通信","801780":"银行","801790":"非银金融","801890":"机械设备","801950":"煤炭",
"801960":"石油石化","801970":"环保","801980":"美容护理"}
VERSION = "sw_l1_2021"


def fetch_snapshot() -> pd.DataFrame:
    rows = []
    for code, name in SW_L1.items():
        df = ak.index_component_sw(symbol=code)
        df["industry_code"] = code
        df["industry_name"] = name
        rows.append(df[["证券代码", "证券名称", "计入日期", "industry_code", "industry_name"]])
        time.sleep(1.2)
    out = pd.concat(rows, ignore_index=True)
    out["证券代码"] = out["证券代码"].astype(str).str.zfill(6)
    return out


def to_records(snap: pd.DataFrame) -> list[dict]:
    records = []
    for code, name in SW_L1.items():
        records.append(dict(kind="asset_info", table="asset", asset_key=f"sw_l1:{code}",
                            asset_type="industry", name=name))
    for _, r in snap.iterrows():
        records.append(dict(kind="membership", table="asset_membership",
                            group_key=f"sw_l1:{r['industry_code']}",
                            asset_key=f"stock:{r['证券代码']}",
                            effective_from=str(r["计入日期"]),
                            classification_version=VERSION,
                            weight=None))
    return records


def main() -> None:
    eng = init_db(f"sqlite:///{ROOT / 'data' / 'capobs.db'}")
    snap = fetch_snapshot()
    with Session(eng) as s:
        stats = write_records(s, to_records(snap))
        s.commit()
    print("snapshot rows:", len(snap), "| writes:", stats)
    with Session(eng) as s:
        n = s.query(AssetMembership).filter(
            AssetMembership.classification_version == VERSION).count()
        changed = s.query(AssetMembership).filter(
            AssetMembership.classification_version == VERSION,
            AssetMembership.effective_to.isnot(None)).count()
    print(f"sw_l1 memberships={n} closed_intervals={changed}")


if __name__ == "__main__":
    main()
