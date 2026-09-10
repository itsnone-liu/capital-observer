"""P0 probe: index market data (broad-base / growth / style / industry).

Verifies: historical daily bars for representative indices via >=1 free
entry; industry index system (EM industry boards + SW indices) for sector
ranking. Per design sec.1 first version scope & sec.3 capability matrix.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_lib import Probe, df_artifact  # noqa: E402

import akshare as ak  # noqa: E402

P = Probe("index_market")

# ---- 1. broad-base & style index daily history (EM) --------------------
BASKET = [
    ("000300", "沪深300", "csindex"),
    ("000905", "中证500", "csindex"),
    ("000852", "中证1000", "csindex"),
    ("000016", "上证50", "sse"),
    ("399006", "创业板指", "szse"),
    ("000688", "科创50", "sse"),
    ("000912", "中证2000小盘", "csindex"),  # small-cap style proxy
]

for code, label, issuer in BASKET[:4]:
    def _f(code=code):
        df = ak.index_zh_a_hist(symbol=code, period="daily",
                                start_date="20230801", end_date="20260910")
        return df_artifact(df, "index_market", f"index_daily_{code}",
                           f"akshare:index_zh_a_hist/{code} (upstream: eastmoney)", "eastmoney")
    P.check(f"index_daily_{code}_{label}", "eastmoney", "akshare.index_zh_a_hist", _f,
            expect_min_rows=400, note=f"{issuer}; need 2y+ daily bars")

for code, label, issuer in BASKET[4:]:
    def _f(code=code):
        df = ak.index_zh_a_hist(symbol=code, period="daily",
                                start_date="20240101", end_date="20260910")
        return df_artifact(df, "index_market", f"index_daily_{code}",
                           f"akshare:index_zh_a_hist/{code}", "eastmoney")
    P.check(f"index_daily_{code}_{label}", "eastmoney", "akshare.index_zh_a_hist", _f,
            expect_min_rows=300, note=f"{issuer}")

# ---- 2. backup source (sina) — independent upstream verification -------
def _sina():
    df = ak.stock_zh_index_daily(symbol="sh000300")
    return df_artifact(df, "index_market", "index_daily_000300_sina",
                       "akshare:stock_zh_index_daily/sh000300 (upstream: sina)", "sina")
P.check("index_daily_000300_sina_backup", "sina", "akshare.stock_zh_index_daily", _sina,
        expect_min_rows=1000, note="independent upstream vs eastmoney")

# ---- 3. EM industry board list (sector universe) -----------------------
def _boards():
    df = ak.stock_board_industry_name_em()
    return df_artifact(df, "index_market", "industry_boards_em",
                       "akshare:stock_board_industry_name_em (upstream: eastmoney)", "eastmoney")
P.check("industry_board_list", "eastmoney", "akshare.stock_board_industry_name_em", _boards,
        expect_min_rows=60, note="EM ~86 industry boards; sector universe for ranking")

# ---- 4. EM industry board historical bars (one sample board) -----------
def _board_hist():
    df = ak.stock_board_industry_hist_em(symbol="半导体",
                                         start_date="20240101", end_date="20260910",
                                         period="日k", adjust="")
    return df_artifact(df, "index_market", "industry_board_hist_semiconductor",
                       "akshare:stock_board_industry_hist_em (upstream: eastmoney)", "eastmoney")
P.check("industry_board_history", "eastmoney", "akshare.stock_board_industry_hist_em", _board_hist,
        expect_min_rows=300, note="board-level daily bars for sector trend/diffusion")

# ---- 5. SW (Shenwan) industry index system ------------------------------
def _sw_list():
    fn = getattr(ak, "sw_index_first_info", None) or getattr(ak, "index_analysis_sw", None)
    if fn is None:
        raise KeyError("no sw index list api in this akshare version")
    df = fn() if getattr(ak, "sw_index_first_info", None) else fn(symbol="一级行业", period="day")
    return df_artifact(df, "index_market", "sw_industry_list",
                       f"akshare:{fn.__name__} (upstream: swsresearch)", "swsresearch")
P.check("sw_industry_list", "swsresearch", "akshare.sw_index_first_info", _sw_list,
        expect_min_rows=20, note="SW L1 industry classification; EM boards are the alternative")

def _sw_hist():
    fn = getattr(ak, "index_hist_sw", None)
    if fn is None:
        raise KeyError("no index_hist_sw in this akshare version")
    df = fn(symbol="801080", period="day")
    return df_artifact(df, "index_market", "sw_industry_hist_801080",
                       f"akshare:{fn.__name__} (upstream: swsresearch)", "swsresearch")
P.check("sw_industry_history", "swsresearch", "akshare.index_hist_sw", _sw_hist,
        expect_min_rows=500, note="SW electronics index full history")

P.finish({
    "goal": "broad/growth/style/industry index bars, 2y+ target",
    "candidate_first_batch": "沪深300/中证500/中证1000/上证50/创业板指/科创50",
})
