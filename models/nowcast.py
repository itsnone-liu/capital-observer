"""Weekly industry-exposure nowcast for active funds (design sec.7.4).

r_f ≈ Σ_k w_k · r_k + ε, estimated with:
  - non-negativity, equity-range-by-type constraints
  - deviation penalty from latest disclosed industry allocation (prior)
  - outputs in-sample fit quality; unknown exposure reported, never zero-filled

Estimates are L2-baseline; L1 = prior-holdings-continuation baseline;
L3 = wide grid (in scenario engine). scipy SLSQP; deterministic seed.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from storage.models import FactObservation

WEEKLY = 5


def _series(session: Session, subject: str, metric: str) -> dict[str, float]:
    rows = session.execute(
        select(FactObservation.effective_at, FactObservation.value)
        .where(FactObservation.subject_key == subject,
               FactObservation.metric == metric,
               FactObservation.quality_status.in_(("valid", "degraded")))
        .order_by(FactObservation.effective_at, FactObservation.id)).all()
    out: dict[str, float] = {}
    for d, v in rows:  # later rows supersede (append-only revision chain)
        out[str(d)] = float(v)
    return out


def _daily_returns(series: dict[str, float], start: str) -> dict[str, float]:
    items = sorted((d, v) for d, v in series.items() if d >= start and len(d) == 10)
    rets = {}
    for (d0, v0), (d1, v1) in zip(items, items[1:]):
        if v0 > 0:
            rets[d1] = v1 / v0 - 1.0
    return rets


def nowcast_fund(session: Session, fund_code: str, industries: list[str],
                 prior: dict[str, float] | None = None,
                 window: int = 120, equity_range: tuple[float, float] = (0.5, 0.95),
                 lambda_prior: float = 20.0) -> dict:
    """Return industry-exposure estimate + fit diagnostics.

    prior: latest disclosed industry weights {industry_code: weight}
           (None -> uninformative prior, quality lowered).
    """
    nav = _series(session, f"etf:{fund_code}", "etf_nav") or \
        _series(session, f"fund:{fund_code}", "fund_nav")
    if len(nav) < window // 2:
        return {"status": "insufficient_nav", "obs": len(nav)}
    fr = _daily_returns(nav, "0000-01-01")
    dates_all = sorted(fr)
    use_dates = dates_all[-window:]
    X_cols = {}
    for ind in industries:
        s = _series(session, f"industry:{ind}", "sw_close")
        r = _daily_returns(s, "0000-01-01")
        X_cols[ind] = r
    # common dates with >=80% industry coverage
    common = [d for d in use_dates
              if sum(1 for r in X_cols.values() if d in r) >= 0.8 * len(industries)]
    if len(common) < 40:
        return {"status": "insufficient_overlap", "common_days": len(common)}
    y = np.array([fr[d] for d in common])
    X = np.array([[X_cols[ind].get(d, 0.0) for ind in industries] for d in common])
    n_ind = len(industries)
    w0 = np.full(n_ind, (sum(prior.values()) if prior else 0.8) / max(n_ind, 1))
    if prior:
        w0 = np.array([prior.get(ind, 0.0) for ind in industries])
        w0 = np.clip(w0, 0, None)
        if w0.sum() > 0:
            w0 = w0 / w0.sum() * np.mean(equity_range)
    lam = lambda_prior * (1.0 if prior else 0.0)
    w_prior = np.array([prior.get(ind, 0.0) for ind in industries]) if prior else np.zeros(n_ind)

    def obj(w):
        resid = y - X @ w
        fit = float(resid @ resid)
        dev = float(((w - w_prior) ** 2).sum())
        return fit + lam * dev / n_ind

    cons = [{"type": "ineq", "fun": lambda w: w.sum() - equity_range[0]},
            {"type": "ineq", "fun": lambda w: equity_range[1] - w.sum()}]
    res = minimize(obj, w0, bounds=[(0.0, 1.0)] * n_ind, constraints=cons,
                   method="SLSQP", options={"maxiter": 200, "ftol": 1e-10})
    w = res.x
    yhat = X @ w
    ss_res = float(((y - yhat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    weekly = X[-WEEKLY:] @ w if len(common) >= WEEKLY else None
    exposures = {ind: round(float(v), 4) for ind, v in zip(industries, w) if v >= 0.005}
    return {
        "status": "ok",
        "fund": fund_code,
        "window_days": len(common),
        "last_date": common[-1],
        "equity_sum": round(float(w.sum()), 3),
        "industry_exposure": exposures,
        "unknown_equity": round(max(0.0, float(w.sum()) - sum(exposures.values())), 3),
        "fit_r2_in_sample": round(r2, 3) if r2 is not None else None,
        "resid_vol_daily": round(float(np.std(y - yhat, ddof=1)), 5),
        "scenario": "L2_baseline",
        "range_type": "scenario_envelope",
        "assumptions": [
            "收益由申万一级行业指数线性近似（风格/个股alpha进入残差）",
            f"股票仓位约束[{equity_range[0]},{equity_range[1]}]（偏股混合假设）",
            "披露先验偏离惩罚λ=20（季度披露→周频外推的主要正则）",
        ],
        "quality_flags": ([] if prior else ["无披露先验：估计仅识别性(identification_bound)"]),
        "invalidation": "下期完整披露验证暴露方向；R²<0.2时降级为research",
    }
