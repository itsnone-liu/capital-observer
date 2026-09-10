"""Cost engine (P4, design sec.9): batch-model trading cost estimates.

gross_buy = A × Σ_k max(Δw_k, 0);  gross_sell = A × Σ_k max(-Δw_k, 0)
cost = commission_rate × (gross_buy + gross_sell)
     + impact_coef(φ) × (gross_buy + gross_sell)     [φ = trade/turnover share]

Impact is a piecewise function of the trade size relative to the sector's
own ADV (participation), with a sensitivity grid. All outputs are estimates:
scenario_id, range_type, assumptions mandatory (design red line).
"""
from __future__ import annotations

import numpy as np
from sqlalchemy.orm import Session

from models.nowcast import _series
from storage.models import FactObservation

# pre-registered defaults (documented, not tuned)
COMMISSION_BPS = 2.5          # roundtrip commission+fees, bps
IMPACT_GRID = {"low": 0.5, "base": 1.0, "high": 2.0}   # multiplier on impact fn


def participation_impact(participation: float) -> float:
    """Square-root impact in bps: 10bps at 1% participation, sqrt scaling."""
    return 10.0 * np.sqrt(max(participation, 0.0) / 0.01)


def fund_rebalance_cost(session: Session, fund: str, weights_t0: dict[str, float],
                        weights_t1: dict[str, float], aum: float,
                        commission_bps: float = COMMISSION_BPS,
                        impact_scenario: str = "base") -> dict:
    """Estimate cost of moving disclosed/estimated weights t0 -> t1.

    Sector ADV comes from SW industry turnover (yuan_100m) facts; if a
    sector's ADV is missing its impact is reported unknown (None), not zero.
    """
    ks = set(weights_t0) | set(weights_t1)
    gross_buy = gross_sell = 0.0
    legs = []
    impact_coef = IMPACT_GRID.get(impact_scenario, 1.0)
    unknown_adv = []
    for k in ks:
        dw = weights_t1.get(k, 0.0) - weights_t0.get(k, 0.0)
        if abs(dw) < 1e-4:
            continue
        notional = abs(dw) * aum
        if dw > 0:
            gross_buy += notional
        else:
            gross_sell += notional
        adv = _series_adv(session, k)
        if adv is None:
            unknown_adv.append(k)
            impact_bps = None
        else:
            participation = notional / (adv * 1e8) if adv > 0 else None
            impact_bps = (round(float(participation_impact(participation)
                                         * impact_coef), 2)
                          if participation is not None else None)
        legs.append({"industry": k, "delta_weight": round(dw, 4),
                     "notional_yuan": round(notional), "impact_bps": impact_bps})
    known = [l for l in legs if l["impact_bps"] is not None]
    turnover = gross_buy + gross_sell
    commission_cost = turnover * commission_bps / 1e4
    impact_cost = sum(l["notional_yuan"] * l["impact_bps"] / 1e4 for l in known)
    return {
        "data": {
            "fund": fund,
            "gross_buy_yuan": round(gross_buy), "gross_sell_yuan": round(gross_sell),
            "turnover_yuan": round(turnover),
            "commission_cost_yuan": round(commission_cost),
            "impact_cost_yuan": round(impact_cost),
            "total_cost_yuan": round(commission_cost + impact_cost),
            "cost_bps_of_aum": round((commission_cost + impact_cost) / aum * 1e4, 2) if aum else None,
            "legs": legs,
            "industries_without_adv": unknown_adv,
        },
        "as_of": None,
        "coverage": f"{len(known)}/{len(legs)}调仓腿有行业ADV（{len(unknown_adv)}条无ADV仅计佣金）",
        "denominator": f"AUM={aum/1e8:.1f}亿；行业ADV=申万20日均成交额；佣金{commission_bps}bps",
        "warnings": [],
        "scenario_id": f"L2_cost_{impact_scenario}",
        "range_type": "scenario_envelope",
        "assumptions": [
            "冲击=10bps×sqrt(参与率/1%)（预注册平方根形式）",
            f"冲击系数×{impact_coef}（{impact_scenario}档）",
            "无ADV行业冲击未计（未知不填零），仅佣金",
        ],
        "invalidation": "实际成交数据不可得（免费源限制）；模型仅情景推断",
    }


def _series_adv(session: Session, industry: str, window: int = 20) -> float | None:
    """ADV in 亿元 for an industry key: SW code directly, or CSRC class summed."""
    import json
    from pathlib import Path
    keys = [industry]
    if not industry.startswith("80"):
        mapping = json.loads((Path(__file__).resolve().parents[1]
                              / "configs" / "cnind_sw_map.json").read_text())
        keys = [k for k in mapping.get(industry, []) if str(k).startswith("80")]
    total, found = 0.0, 0
    for k in keys:
        rows = session.query(FactObservation.value).filter(
            FactObservation.subject_key == f"industry:{k}",
            FactObservation.metric == "sw_amount",
            FactObservation.quality_status == "valid"
        ).order_by(FactObservation.effective_at.desc()).limit(window).all()
        if rows:
            total += float(np.mean([r[0] for r in rows]))
            found += 1
    if not found:
        return None
    return total  # 亿元 (sum over mapped SW industries when CSRC class given)
