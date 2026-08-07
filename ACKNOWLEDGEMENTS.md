# 设计参考与致谢

Frontier Radar 的实现结合了本项目自己的证据纪律、隐私边界和低频访问约束，也从下列
开源项目公开展示的产品能力与工程取舍中获得了设计启发。这里记录的是**设计参考**，不是
含糊的“业界惯例”归因。

截至 2026-08-06，当前仓库没有已知从下列项目直接复制、翻译或改写的源码；相关功能均按
本项目约束独立实现。因此，下表的许可证是上游项目的许可证记录，不表示 Frontier Radar
自动继承了相同许可证。若以后直接复用代码或其他受版权保护的材料，必须在合入时补充精确
文件来源、上游版本或提交、版权声明和许可证文本，不能只依赖本页的概括性致谢。

| 上游项目 | 本项目吸收的设计启发 | 对应实现 | 上游许可证（核对日） |
| --- | --- | --- | --- |
| [Miniflux](https://github.com/miniflux/v2) | 对 RSS/Atom 抓取使用条件请求、HTTP 校验器、受控轮询与 URL 清理；本项目在此基础问题上进一步加入同逻辑日复用、有界下载、失败冷却和 Reddit 硬预算。 | `scripts/http_fetch_state.py`、`scripts/fetch_l1.py`、`scripts/reddit_audit_baseline.py` | [Apache-2.0](https://github.com/miniflux/v2/blob/main/LICENSE)（2026-08-06） |
| [changedetection.io](https://github.com/dgtlmoon/changedetection.io) | 把网页变化而非页面全文当作监测对象，并针对价格页、状态页等非 RSS 页面保留可比较历史；本项目采用内容哈希、页面快照和候选变化审计，但不复用其抓取器或 UI。 | `scripts/fetch_l1.py`、`scripts/audit_source_partitions.py` | [Apache-2.0](https://github.com/dgtlmoon/changedetection.io/blob/master/LICENSE)（2026-08-06） |
| [NewsBlur](https://github.com/samuelclay/NewsBlur) | 信源健康、故事版本演化、分类/过滤和每日阅读工作流应当是长期状态，而不是一次性摘要；本项目据此强化了健康账本、故事 lineage 和可回放分类。 | `scripts/build_source_health.py`、`scripts/build_story_lineage.py`、`scripts/backtest_classification.py` | [MIT](https://github.com/samuelclay/NewsBlur/blob/main/LICENSE.md)（2026-08-06） |
| [Huginn](https://github.com/huginn/huginn) | 将网络监测器拆成独立 Agent，并让事件沿有向图连接处理阶段；本项目从这种分段方式得到启发，把采集、派生和收尾做成单独的可审计步骤、显式记录单步失败，而非移植 Huginn 的 Agent 架构。 | `scripts/daily_orchestrator.py`、`scripts/finalize_daily.py` | [MIT](https://github.com/huginn/huginn/blob/master/LICENSE)（2026-08-06） |

## 维护规则

- 新增受外部项目启发的能力时，同一变更必须更新本页，写清楚“借鉴了什么”和“没有借鉴什么”。
- 仅看到相似功能不能反向声称对方是灵感来源；应记录实际查阅过并影响设计决策的项目。
- 概念借鉴与代码复用分开处理。代码、文档、图标、测试数据或其他材料一旦直接复用，必须
  按上游许可证履行完整义务，并在对应文件附近保留可追溯标注。
- 上游许可证可能变化；表中日期只说明最后核对时间，正式复用前仍需重新检查具体版本。
