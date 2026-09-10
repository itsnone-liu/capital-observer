"""Scenario engine (P3) — L1 cautious / L2 baseline / L3 extended.

Design sec.8: three tiers are NOT bullish/bearish levels and need not be
monotone. Each tier is a versioned config with parameters, supporting
evidence, applicable fund types and invalidation conditions. Estimates are
always labeled with range_type; no probabilities without calibration.

Diffusion (sec.7.5): direction counted only beyond a pre-registered
significance threshold; up/down/flat/unidentifiable reported with explicit
denominators.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from storage.models import Asset, FactObservation

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "configs" / "scenarios"

# ---- pre-registered thresholds (P0/research; NOT tuned on test data) ------
DIFFUSION_THRESHOLD = 0.02   # |20d return| >= 2% counts as directional; else flat
DEFAULT_CONFIGS = {
    "L1": {
        "name": "谨慎",
        "share_price_basis": "quarter_end_nav",   # flows valued at quarter-end NAV only
        "turnover_assumption": "hold_to_next_disclosure",
        "range_type": "identification_bound",
        "notes": "有限持仓延续、样本内估计、宽区间；不做任何日内推断",
        "invalidation": "基金类型变更/招募说明书投资范围变更",
    },
    "L2": {
        "name": "基准",
        "turnover_assumption": "moderate_rebalance_within_type",
        "retention_window": 20,
        "range_type": "scenario_envelope",
        "notes": "中等换手、约束调仓、适度持仓保留；行业权重约束在基金类型允许范围内",
        "invalidation": "样本外验证显示暴露MAE高于持仓延续基线",
    },
    "L3": {
        "name": "扩展",
        "group_extrapolation": "by_fund_type",
        "turnover_assumption": "wide_grid",
        "turnover_grid": [0.05, 0.15, 0.30],
        "concordance_grid": ["weak", "medium"],
        "range_type": "scenario_envelope",
        "notes": "分组外推与更宽换手/同向性假设网格；未覆盖资金不默认与样本一致",
        "invalidation": "证据不足时不生成本档（允许缺档）",
    },
}


def load_scenario_configs() -> dict:
    f = CONFIGS / "tiers.json"
    if f.exists():
        return json.loads(f.read_text())
    CONFIGS.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(DEFAULT_CONFIGS, ensure_ascii=False, indent=2))
    return DEFAULT_CONFIGS


def industry_diffusion(session: Session, window: int = 20,
                       threshold: float = DIFFUSION_THRESHOLD) -> dict:
    """Sec.7.5 diffusion across SW industries (equal-weight denominator).

    Pre-registered: directional iff |return| >= threshold; else flat.
    Denominator = industries WITH data; coverage explicit.
    """
    rows = session.execute(
        select(FactObservation.subject_key).distinct()
        .where(FactObservation.metric == "sw_close")).scalars().all()
    up = down = flat = 0
    detail = []
    for subj in rows:
        q = (select(FactObservation.effective_at, FactObservation.value)
             .where(FactObservation.subject_key == subj,
                    FactObservation.metric == "sw_close",
                    FactObservation.quality_status == "valid")
             .order_by(FactObservation.effective_at))
        df = list(session.execute(q).all())
        if len(df) <= window:
            flat += 1  # insufficient data -> not counted as directional; reported
            continue
        # latest revision per date
        seen = {}
        for d, v in df:
            seen[d] = v
        vals = sorted(seen.items())
        r = vals[-1][1] / vals[-1 - window][1] - 1.0
        if r >= threshold:
            up += 1
        elif r <= -threshold:
            down += 1
        else:
            flat += 1
        detail.append({"industry": subj.split(":")[-1], f"ret_{window}d": r})
    n = len(rows)
    return {
        "data": {"up": up, "down": down, "flat": flat, "n": n,
                 "up_ratio": up / n if n else None, "down_ratio": down / n if n else None,
                 "net_diffusion": (up - down) / n if n else None,
                 "detail_sorted": sorted(detail, key=lambda x: x[f"ret_{window}d"], reverse=True)},
        "as_of": None, "coverage": f"{n}个申万一级行业",
        "denominator": f"等权分母={n}个有数据行业；方向阈值|{window}日收益|≥{threshold:.0%}（预注册，非调参）",
        "warnings": [],
        "scenario_id": "FACT",  # this is a fact-derived statistic, not an estimate
    }
