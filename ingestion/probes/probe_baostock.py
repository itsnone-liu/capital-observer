"""P0 probe: baostock (independent upstream, own TCP protocol — not affected
by eastmoney WAF). Provides index/stock daily bars + CSRC industry map.
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import Probe, save_artifact, rate_limit  # noqa: E402

import baostock as bs  # noqa: E402
import pandas as pd  # noqa: E402

P = Probe("baostock")

lg = bs.login()
P.check("login", "baostock", "bs.login", lambda: [("login", lg.error_code, lg.error_msg)],
        note=f"error_code={lg.error_code} msg={lg.error_msg}")


def _bars():
    rs = bs.query_history_k_data_plus(
        "sh.000300", "date,code,close,volume,amount",
        start_date="2023-08-01", end_date="2026-09-10", frequency="d", adjustflag="2")  # 前复权
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    buf = io.StringIO(); df.to_csv(buf, index=False)
    meta = save_artifact("baostock", "bs_index_000300", buf.getvalue(),
                         "baostock:query_history_k_data_plus/sh.000300", "baostock", "text/csv")
    return df, meta
P.check("index_daily_000300", "baostock", "bs.query_history_k_data_plus", _bars,
        expect_min_rows=400, note="independent bars vs EM/sina for reconciliation")

def _industry():
    rs = bs.query_stock_industry()
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    buf = io.StringIO(); df.to_csv(buf, index=False)
    meta = save_artifact("baostock", "bs_stock_industry", buf.getvalue(),
                         "baostock:query_stock_industry", "baostock", "text/csv")
    return df, meta
P.check("stock_industry_csrc_map", "baostock", "bs.query_stock_industry", _industry,
        expect_min_rows=4000, note="CSRC industry classification; alternative to SW/EM boards")

def _margin_proxy():
    # baostock has no margin data; check balance sheet path exists at all (negative probe)
    rs = bs.query_profit_data(code="sh.600000", year=2025, quarter=1)
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    buf = io.StringIO(); df.to_csv(buf, index=False)
    meta = save_artifact("baostock", "bs_profit_600000", buf.getvalue(),
                         "baostock:query_profit_data/sh.600000", "baostock", "text/csv")
    return df, meta
P.check("fundamental_data_sample", "baostock", "bs.query_profit_data", _margin_proxy,
        note="fundamentals coverage sanity (not core for capital-flow, context layer)")

try:
    bs.logout()
except Exception:
    pass

P.finish({
    "goal": "independent upstream for bars + industry map; WAF-resistant",
})
