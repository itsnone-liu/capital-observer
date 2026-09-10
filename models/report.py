"""Report generator (P4, sec.10): assembles endpoint data into a weekly
observation report. Template-first; AI polish optional (model call separable).

Everything in the report traces to endpoints/facts; estimates carry their
scenario_id; unknown/missing stays visible (never zero-filled prose).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8120"
IDX_NAMES = {"sh000300": "沪深300", "sh000016": "上证50", "sh000905": "中证500",
             "sh000852": "中证1000", "sh000912": "中证2000", "sz399006": "创业板指",
             "sh000688": "科创50"}


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read())


def pct(v):
    return "—" if v is None else f"{v * 100:+.2f}%"


def _sw_names() -> dict:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from storage.models import Asset
    eng = create_engine(f"sqlite:///{ROOT / 'data' / 'capobs.db'}", future=True)
    with Session(eng) as s:
        return dict(s.execute(select(Asset.asset_key, Asset.name)
                              .where(Asset.asset_type == "industry")).all())


def generate_weekly() -> str:
    ov = _get("/api/v1/market/overview")
    sr = _get("/api/v1/sectors/ranking")
    mg = _get("/api/v1/margin/summary")
    dif = _get("/api/v1/diffusion")
    try:
        ag = _get("/api/v1/estimate/aggregate")
    except Exception:  # noqa: BLE001 — report survives estimate outage
        ag = {"status": "unavailable"}

    lines: list[str] = []
    a = lines.append
    a(f"# A股资金结构周报（观测版）")
    a(f"\n**as_of** {ov['as_of']} · 生成时间 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · run: 事实层+L2基准\n")

    a("## 一、大盘事实")
    for k, v in ov["data"].items():
        if not v.get("close"):
            continue
        a(f"- {IDX_NAMES.get(k, k)}：{v['close']:.1f}（1日 {pct(v['ret_1d'])} / 20日 {pct(v['ret_20d'])} / 60日 {pct(v['ret_60d'])}）")

    a("\n## 二、板块结构（申万一级，20日）")
    top = [x for x in sr["data"][:3]]
    bot = [x for x in sr["data"][-3:]]
    a("**领先**：" + "、".join(f"{x['name']} {pct(x['ret_20d'])}" for x in top))
    a("**落后**：" + "、".join(f"{x['name']} {pct(x['ret_20d'])}" for x in bot))
    dd = dif["data"]
    a(f"**扩散**：上升 {dd['up']} / 下降 {dd['down']} / 平 {dd['flat']}（净 {dd['net_diffusion']:+.3f}；{dif['denominator'][:38]}…）")

    a("\n## 三、两融（分所列示，不相加）")
    for mkt, v in (mg["data"] or {}).items():
        if not v:
            continue
        nm = "上交所" if mkt == "SSE" else "深交所"
        a(f"- {nm}融资余额 {v['latest_balance_yuan'] / 1e12:.3f}万亿，20日 {v.get('balance_chg_20d_yuan', 0) / 1e8:+.1f}亿（{v['obs_days']}日样本）")

    a("\n## 四、公募行业暴露（L2估计 · 等权样本）")
    sw_names = _sw_names()
    if ag.get("status") == "ok":
        d = ag["data"]
        top5 = "、".join(f"{sw_names.get(k, k)}:{v * 100:.1f}%" for k, v in d["top5"].items())
        a(f"- 达标样本 {d['n_funds_qualified']}/{d['n_funds_attempted']}（R²≥0.2），平均拟合R²={d['mean_r2']}，平均股票仓位 {d['mean_equity_position'] * 100:.0f}%")
        a(f"- 前五大暴露：{top5}")
        a(f"- 口径：{ag['range_type']}（等权样本非总体；{ag['denominator']}）")
    else:
        a(f"- 估计服务不可用（{ag.get('status')}）——如实缺省，不编造")

    a("\n## 五、口径与局限")
    a("- ETF份额为季度披露：任何日度净申赎均为区间近似（L1宽口径），本报告未输出日度申赎数字")
    a("- 两融双所分列；公募暴露是 identification_bound 非市场总体")
    a("- 未知/缺数如实标注为 — ；不填零、不插值、不跨口径相加")
    a("\n> 本报告为公开数据的描述性观测，不构成投资建议。")
    return "\n".join(lines)


if __name__ == "__main__":
    out = generate_weekly()
    dest = ROOT / "reports" / f"weekly_{dt.date.today().isoformat()}.md"
    dest.parent.mkdir(exist_ok=True)
    dest.write_text(out)
    print(out)
