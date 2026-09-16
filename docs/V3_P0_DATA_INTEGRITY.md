# V3 P0 数据时间完整性止血（2026-09-16）

## 审计结论

- `fact_observation` 已具备 append-only、revision chain、source/unit/ingested_at；
- 但存量 1,316,663 条事实的 `published_at` 均由 adapter 复制
  `effective_at`，不是可验证的真实披露时间；
- 无 `available_at` 列；旧 `/context` 把 observation_date 当 available_at 并硬标 fresh；
- membership 有 effective_from/to schema，但摄取从不关闭旧区间。

因此，pre-V3 资金历史不得称严格 PIT。

## P0 止血决策

1. 新增 `FactObservation.available_at`；
2. 存量伪 `published_at` 全部清空，不猜、不回填；存量 `available_at` 保持 NULL；
3. JobRunner 对新摄取：只有 adapter 明确设置 `published_at_verified=True` 才保留
   published_at，否则清空；available_at 默认实际抓取 UTC 时间；
4. 同值事实刷新时允许补上首次真实 available_at；
5. `/context` 只有 latest record 的 available_at 已知时才将通道视为可用，否则
   limitation=availability_unknown，整体按 unknown/coverage 降级；
6. membership 换组时关闭旧开放区间，并开启新分类区间。

## 有意推迟到 P2

- `as_of` 历史 context、batch context、observations API；
- 股票历史行业映射回补（今天名单不能倒灌过去）；
- 风格层、市场宽度、行业 ETF/两融覆盖；
- 日期格式统一迁移与真正交易日历。

原因：在发布时间/可用时间尚不真实时上线历史 as_of API，只会生成更隐蔽的伪 PIT。

## 恢复严格 PIT 的条件

- 每个 source 建立 publication rule（官方具体发布时间、T+1保守规则或 unknown）；
- 对回补历史逐源证明并回填 available_at；无法证明的只进入 hindsight view；
- snapshot 查询必须 `available_at <= as_of`，revision 也必须在当时已存在。
