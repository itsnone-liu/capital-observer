"""etf_share 适配器离线单测（不碰网络；fetch 层由 P0 验收覆盖）。"""
from __future__ import annotations

import pandas as pd

from ingestion.adapters.etf_share import (
    SSEETFShareAdapter,
    SZSEETFShareAdapter,
    trading_days_since,
)


def _csv(df: pd.DataFrame) -> dict:
    import io
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return {"payload": buf.getvalue(), "url": "t", "content_type": "text/csv"}


def test_sse_parse_and_empty():
    a = SSEETFShareAdapter(dates=["20260914"])
    df = pd.DataFrame({"基金代码": ["510300", "588000"],
                       "基金名称": ["x", "y"],
                       "基金份额": [23403187700.0, 9123456789.0]})
    recs = a.parse("20260914", _csv(df))
    assert len(recs) == 2
    r = recs[0]
    assert r["subject_key"] == "etf:510300" and r["metric"] == "etf_total_shares"
    assert r["effective_at"] == "2026-09-14" and r["unit"] == "shares"
    assert a.parse("20260915", {"payload": "", "url": "t", "content_type": "text/csv"}) == []


def test_szse_parse_single_and_multichunk_sep():
    b = SZSEETFShareAdapter("20260903", "20260915")
    df = pd.DataFrame({"日期": ["2026-09-11", "2026-09-11"],
                       "基金代码": ["159915", "159949"],
                       "基金份额": [19291454936.0, 123.0]})
    recs = b.parse("x", _csv(df))
    assert {r["asset_key"] for r in recs} == {"159915", "159949"}
    assert recs[0]["effective_at"] == "2026-09-11"
    # 多段 payload 用 \x00 分隔（CSV 内含换行，绝不能按 \n 拼/拆）
    df2 = pd.DataFrame({"日期": ["2026-09-14"], "基金代码": ["159901"],
                        "基金份额": [456.0]})
    two = _csv(df)
    two["payload"] = two["payload"] + "\x00" + _csv(df2)["payload"]
    recs2 = b.parse("x", two)
    assert {r["asset_key"] for r in recs2} == {"159915", "159949", "159901"}
    # 重复（日期,代码）跨段也会被去重（幂等）
    three = _csv(df)
    three["payload"] = three["payload"] + "\x00" + _csv(df)["payload"]
    assert len(b.parse("x", three)) == 2
    assert b.parse("x", {"payload": "", "url": "t", "content_type": "text/csv"}) == []


def test_trading_days_since_inclusive_and_weekend_skip():
    assert trading_days_since("2026-09-12", "2026-09-15") == ["20260914", "20260915"]  # 六日跳过
    assert trading_days_since("2026-09-14", "2026-09-15") == ["20260914", "20260915"]  # 含起点
