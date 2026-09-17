#!/usr/bin/env python3
"""研究背景批量导出：strict-PIT 事实长表 + ETF 新增暴露 + 成员有效期。

输出（--out 目录）：
  facts.csv               事实长表（subject_key, metric, value, effective_at, available_at）
  etf_new_exposure.csv    同日份额×净值（不成对即缺行）
  membership_history.csv  申万L1全量生效区间
  manifest.json           as_of/指标/行数/边界声明

用法:
  python3 scripts/export_research_context.py --as-of 2026-09-17T00:00:00 \
      --start 2025-01-01 --out output/research_context
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobs.backfill import DB_URL  # noqa: E402
from research.context_export import (  # noqa: E402
    DEFAULT_METRICS, etf_new_exposure, export_facts, export_membership_history,
    fact_coverage)
from storage.models import init_db  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DB_URL)
    p.add_argument("--as-of", default=None, help="PIT 截止（ISO 时间戳或日期）")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    p.add_argument("--membership-version", default="sw_l1_2021")
    p.add_argument("--out", default="output/research_context")
    a = p.parse_args()
    metrics = [m.strip() for m in a.metrics.split(",") if m.strip()]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    engine = init_db(a.db)
    with Session(engine) as s:
        facts = export_facts(s, metrics, start=a.start, end=a.end, as_of=a.as_of)
        exposure = etf_new_exposure(facts)
        membership = export_membership_history(s, a.membership_version)
        coverage = fact_coverage(s, metrics, start=a.start, end=a.end, as_of=a.as_of)
    facts.to_csv(out / "facts.csv", index=False)
    exposure.to_csv(out / "etf_new_exposure.csv", index=False)
    membership.to_csv(out / "membership_history.csv", index=False)
    coverage.to_csv(out / "fact_coverage.csv", index=False)
    manifest = {
        "as_of": a.as_of, "metrics": metrics, "start": a.start, "end": a.end,
        "membership_version": a.membership_version,
        "fact_rows": len(facts), "etf_pair_rows": len(exposure),
        "membership_rows": len(membership),
        "metrics_without_availability": coverage.query(
            "rows_with_availability == 0")["metric"].tolist(),
        "view": "strict_pit",
        "availability_method": "available-at-background-conservative-v1 "
                               "(价格量额=T日15:30；份额净值=T日22:00；margin_fin_*未回补)",
        "limitations": [
            "available_at 未知或晚于 as_of 的事实已排除，不猜测",
            "etf_new_exposure = 官方日度份额×单位净值（申赎口径新增暴露），非二级市场现金买入",
            "membership effective_to 为空=至今有效；消费方必须自检同 code 区间不重叠",
            "sf_* 板块大单代理自 2026-03 起才有，早期背景为 unknown 而非零",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({k: manifest[k] for k in
                      ("as_of", "fact_rows", "etf_pair_rows", "membership_rows")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
