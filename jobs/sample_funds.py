"""First-batch active-equity fund sample (config: 50-100 funds).

Sampling rule (pre-registered, first_batch.yaml): from purchasable
active-equity funds (混合/股票型 excluding index/ETF/LOF/QDII), take a
deterministic hash-based sample — no discretionary picking. Sample-bias
noted downstream: no AUM weighting available -> aggregate estimates are
labeled identification_bound, not market totals.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sample_fund_codes(n: int = 60, seed_label: str = "capobs-first-batch-v1") -> list[str]:
    import akshare as ak
    df = ak.fund_purchase_em()
    eq = df[df["基金类型"].str.contains("混合|股票型", na=False)]
    eq = eq[~eq["基金类型"].str.contains("指数|ETF|联接|LOF|QDII", na=False)]
    codes = sorted(eq["基金代码"].astype(str).tolist())
    # deterministic order-by-hash sample: stable across runs, no cherry-picking
    ranked = sorted(codes, key=lambda c: hashlib.sha256(f"{seed_label}:{c}".encode()).hexdigest())
    return ranked[:n]


def main():
    from ingestion.adapters.fund_data import FundIndustryAllocAdapter, FundNavAdapter
    from ingestion.base import JobRunner
    from jobs.backfill import DB_URL, RAW_ROOT
    import datetime as dt

    codes = sample_fund_codes()
    (ROOT / "data" / "p0_results").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "p0_results" / "fund_sample.json").write_text(
        json.dumps({"rule": "hash-sample of purchasable active equity, seed=capobs-first-batch-v1",
                    "n": len(codes), "codes": codes}, ensure_ascii=False, indent=1))
    print(f"sampled {len(codes)} funds", flush=True)
    r = JobRunner(DB_URL, RAW_ROOT)
    tag = dt.date.today().isoformat()
    s1 = r.run(FundNavAdapter(codes=codes), job_key=f"fund_nav_sample:{tag}:v1")
    print("nav:", s1.get("status"), s1.get("ok"), s1.get("writes"), flush=True)
    s2 = r.run(FundIndustryAllocAdapter(codes=codes, year="2025"), job_key=f"fund_alloc_sample:{tag}:v1")
    print("alloc:", s2.get("status"), s2.get("ok"), s2.get("writes"), flush=True)
    s3 = r.run(FundIndustryAllocAdapter(codes=codes, year="2026"), job_key=f"fund_alloc_sample:{tag}:v1y26")
    print("alloc26:", s3.get("status"), s3.get("ok"), s3.get("writes"), flush=True)
    print("SAMPLE DONE", flush=True)


if __name__ == "__main__":
    main()
