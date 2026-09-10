"""P0 probe: index market data (broad-base / growth / style / industry).

v2: akshare's index_zh_a_hist & board APIs ride 80.push2.* nodes that are
RST-blocked from this network (P0 finding). Switched to em_client (multi-node
rotation, direct secid). Sina + baostock remain independent upstreams.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import Probe, df_artifact  # noqa: E402

import akshare as ak  # noqa: E402
import em_client  # noqa: E402

P = Probe("index_market")

BASKET = [
    ("000300", "沪深300", "csindex"),
    ("000905", "中证500", "csindex"),
    ("000852", "中证1000", "csindex"),
    ("000016", "上证50", "sse"),
    ("399006", "创业板指", "szse"),
    ("000688", "科创50", "sse"),
    ("000912", "中证2000小盘", "csindex"),
]

# ---- 1. broad-base & style index daily history (SINA primary, full hist) ----
SINA_BASKET = {
    "sh000300": "沪深300", "sh000905": "中证500", "sh000852": "中证1000",
    "sh000016": "上证50", "sz399006": "创业板指", "sh000688": "科创50",
    "sh000912": "中证2000小盘",
}
for sym, label in SINA_BASKET.items():
    def _f(sym=sym):
        df = ak.stock_zh_index_daily(symbol=sym)
        return df_artifact(df, "index_market", f"index_daily_{sym}",
                           f"akshare:stock_zh_index_daily/{sym} (upstream: sina)", "sina")
    P.check(f"index_daily_{label}", "sina", "akshare.stock_zh_index_daily", _f,
            expect_min_rows=3000, note="primary source; EM kline blocked (see em_kline_status)")

# ---- 1b. EM direct kline: single fast canary (push2his RST from this host) --
def _emk():
    df = em_client.kline_daily("000300", start="20240101", end="20240110", tries=2)
    return df_artifact(df, "index_market", "em_kline_canary_000300",
                       "em_client:kline/000300 (upstream: eastmoney)", "eastmoney")
P.check("em_kline_status_canary", "eastmoney", "em_client.kline_daily", _emk,
        note="if UNAVAILABLE: use sina/sw/baostock; EM kline is optional")

# ---- 2. independent upstreams: sina (akshare) ---------------------------
def _sina():
    df = ak.stock_zh_index_daily(symbol="sh000300")
    return df_artifact(df, "index_market", "index_daily_000300_sina",
                       "akshare:stock_zh_index_daily/sh000300 (upstream: sina)", "sina")
P.check("index_daily_000300_sina_backup", "sina", "akshare.stock_zh_index_daily", _sina,
        expect_min_rows=1000, note="independent upstream vs eastmoney")

# ---- 3. EM industry board list (direct EM) -------------------------------
def _boards():
    df = em_client.board_list()
    return df_artifact(df, "index_market", "industry_boards_em",
                       "em_client:board_list (upstream: eastmoney)", "eastmoney")
P.check("industry_board_list", "eastmoney", "em_client.board_list", _boards,
        expect_min_rows=60, note="EM industry boards; sector universe for ranking")

# ---- 4. EM industry board historical bars (半导体 BK1036) ------------------
def _board_hist():
    df = em_client.kline_daily("BK1036", start="20240101", end="20260910")
    return df_artifact(df, "index_market", "industry_board_hist_semiconductor",
                       "em_client:kline/BK1036 (upstream: eastmoney)", "eastmoney")
P.check("industry_board_history", "eastmoney", "em_client.kline_daily", _board_hist,
        expect_min_rows=300, note="board-level daily bars for sector trend")

# ---- 5. SW industry index system (secondary classification upstream) ------
def _sw_hist():
    fn = getattr(ak, "index_hist_sw", None)
    if fn is None:
        raise KeyError("no index_hist_sw in this akshare version")
    df = fn(symbol="801080", period="day")
    return df_artifact(df, "index_market", "sw_industry_hist_801080",
                       f"akshare:{fn.__name__} (upstream: swsresearch)", "swsresearch")
P.check("sw_industry_history", "swsresearch", "akshare.index_hist_sw", _sw_hist,
        expect_min_rows=500, note="SW electronics full history")

P.finish({
    "goal": "broad/growth/style/industry index bars, 2y+ target",
    "candidate_first_batch": "沪深300/中证500/中证1000/上证50/创业板指/科创50",
    "p0_finding": "akshare push2-node calls RST from this host; em_client rotation works",
})
