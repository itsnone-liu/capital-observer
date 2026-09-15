# ETF 官方日度份额通道（2026-09-15 上线）

免费交易所直连（无凭据），T 晚清算后发布（T+1 早可得的事实层）。

## 数据道

| 环节 | 内容 |
|---|---|
| 采集 | `ingestion/adapters/etf_share.py`：SSE `fund_etf_scale_sse(date)` 逐日快照；SZSE `fund_scale_daily_szse` 区间 xlsx（≤150天/段，`\x00` 分段） |
| 存储 | `fact_observation`：metric=`etf_total_shares`，unit=shares，subject=`etf:{code}` |
| 刷新 | `scripts/refresh.py` 步骤3.5 增量道：`--skip-etf` 跳过，`--etf-backfill=N` 冷启动回看 |
| API | `GET /api/v1/etf/shares?code=&days=`：同源差分 share_change，`estimated_net_flow=Δ×当日净值`（nav 缺失→null 不外推） |
| 消费 | v2 stock-selector `decide` 自动经 `/api/v1/context` 消费（宽基ETF通道） |

## 契约要点

- **同源差分**：沪/深清算时点不同，绝不跨源算 Δ；
- **T+1 事实**：当日盘中不存在此事实，`/api/v1/context` 里标注 T-1；
- SSE 当日未清算 → 空表（adapter 返回空 payload，0 条，无错误）；
- SZSE 偶发 ConnectionReset → adapter 内 2 次重试即过。

## 踩坑存档

多段 CSV 载荷不能用 `"\n"` join/split——CSV 自身含换行，按行拆会把每行
当独立 CSV、首行数据成表头，几千个互异列名帧 concat 出 帧数×列数 稀疏
矩阵，parse 阶段 CPU 满转假死。必须用数据中不存在的分隔符（`\x00`）。
（`tests/test_etf_share_adapter.py` 有回归测试。）
