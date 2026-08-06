# Frontier Radar

个人用前沿 AI 情报管线：采集**模型发布 / 编程 agent 一线实测 / 订阅定价与降智动态**，产出每日中文简报，并为长期未决问题（"悬案"）做证据记账。

> 本仓库只含公开层（代码 + 主题/信源配置）。研究产出与记忆核心（悬案账本、账号库、简报、专题报告等）为本地私有文件，不入库——见 `.gitignore` 的私有层清单。克隆本仓库即可运行管线，但需自建 `config/claims.yaml` 与 `config/accounts.yaml`（结构见 `scripts/build_digest.py` 的读取逻辑；两个文件缺失时对应栏目为空）。

## 运行

依赖：Python 3.10+，`requests`、`pyyaml`（可选 `anthropic`）。

```bash
python scripts/fetch_l1.py && python scripts/build_digest.py
```

产出 `briefs/YYYY-MM-DD.md`，七个栏目：今日发布／一线实测／定价与额度变动／降智观察／公司动态／悬案更新／新信源候选。全部信源为免登录公开端点（35 个已启用：AINews、HN Algolia、Reddit RSS、Hugging Face API、厂商官网与定价页、服务状态页 JSON、OpenRouter 与 genai-prices 结构化牌价索引、Yahoo 行情、CodexRadar 降智雷达（IQ 序列追踪；其数据仅私有研究引用）、Aider 排行榜、llama.cpp Releases 等），配置在 [config/sources.yaml](config/sources.yaml)，单信源失败不影响整体。结构化牌价索引只用于发现变化，正式判断仍回到厂商原始定价页。

日报合成不会把整个历史账本反复送入 LLM。`build_analysis_context.py` 生成一个有大小上限的当前视图：全部未决悬案只保留目录，高精度命中的悬案才展开证据；仅实体词命中的项目单列为人工候选。弱信号进入按月分片的私有候选箱，正式立案与改判保持人工控制。

- `--llm`：摘要正文由 Claude API 生成（需 `ANTHROPIC_API_KEY` 或 `ant auth login` 登录态；失败或输出被截断时自动回退机械聚合版，逐条链接的机械列表任何情况下都保留）。
- `--window N`：只收录最近 N 天条目（默认 3，用于收敛全历史 feed）。
- `--keep-days N`（fetch_l1）：`data/raw` 自动保留最近 N 天（默认 45）。
- `--refresh-http`（fetch_l1）：忽略本地新鲜期并立即向源站复核；仍使用 ETag/Last-Modified 条件头，也不能绕过 Reddit 的跨进程硬闸门。
- 无 RSS 的站点用页面快照 + 跨日内容哈希比对（`data/state/html_snapshots.json`），只有内容变化时才进简报。

L1 的公开 GET 在私有 `data/state/http_cache/` 保存响应校验器和内容寻址缓存：同一逻辑日的新鲜期内复用原字节，跨日或过期后优先发送 `If-None-Match` / `If-Modified-Since`，收到 304 后继续用已验证正文。HTTP freshness 与应用检查时间分别记账；显式 `max-age`/`Expires` 只会缩短复用窗口，`no-cache`、`must-revalidate`、`no-store` 与 `Vary: *` 均禁止不当复用。默认自适应窗口为 30 分钟至 12 小时，只优化同日重复运行，不跳过次日主抓取。

连续可重试失败采用指数冷却；仅网络错误及 500/502/503/504 可在 12 小时内明确标为 `stale` 复用，其他冷却记为 `deferred`。JSON/XML 正文先通过格式解析才进入可复用缓存，避免把 HTTP 200 的挑战页固化为新鲜响应。网络正文默认最多 15 MB：先检查 `Content-Length`，再以流式读取对实际解压字节执行硬上限；同时每个网络响应的正文读取默认最多 120 秒，由独立 watchdog 到点关闭连接，补足 Requests socket 静默超时不限制整个下载时长的缺口。字节或时间超限均立即关闭且进入失败冷却；前者拒绝旧正文，后者作为传输失败可回退已验证 stale 正文，单信源可用 `fetch_policy.max_download_bytes` / `max_download_seconds` 进一步收紧但不能放宽。多查询信源混合成功与失败记为 `partial`，全部失败记为 `error`。429/503 的 `Retry-After` 同时阻断同一 host 的其他 URL；重定向逐跳关闭自动跟随后处理，每一跳都重新经过 host 冷却与 Reddit 硬闸门。外部重定向只允许 HTTP(S) 默认端口且不得携带用户凭据；每次真实请求前检查全部 A/AAAA 地址，解析失败或任一地址不是公网单播均拒绝、清除旧缓存并进入冷却。只有整条跳转链均为 301/308 且最终正文成功验证后，才把永久目标记入私有 HTTP 状态；任一跳带 `no-store` / `no-cache` / `must-revalidate` / `proxy-revalidate` 或 `Vary: *` 均不记忆，显式 `max-age` / `Expires` 取全链最早截止时间，到期自动回配置地址复核。有效期内直接向目标发送条件请求；302/303/307 不记忆，目标失败后清除记忆并在冷却结束后回到配置地址。这个优化不改写 `config/sources.yaml`，关闭 HTTP 状态缓存时也完全停用。初始 URL 仍由受审的信源配置负责，应用层 DNS 预检也不能替代网络层出站隔离。跨进程同请求与同 host 的联网阶段分别串行，长时间限速等待会刷新带所有者令牌的请求租约。信源健康账本将联网成功、部分成功、缓存复用、失败冷却复用和冷却未请求分开统计。缓存正文按 `--keep-days` 清理，并明确排除在最终产物指纹和私有 Git 备份之外。

### Reddit 信源影子审计

候选 subreddit 不直接加入正式日报。独立审计脚本按 `config/reddit_audit.yaml` 中的
研究、Agent、MLOps、运行时、模态和泛 AI 对照组低频轮换采集 `/new/.rss`，每个社区累计 14 次样本后比较
活跃度、技术/证据文本代理、直接证据链接、跨社区重复、噪声，以及相对现有非 Reddit
L1 的领先时间：

```bash
python scripts/audit_reddit_sources.py
```

原始样本写入 `data/reddit_audit/`，日报之外的阶段报告写入
`reports/reddit-source-audit/`；二者均为私有层。休眠配置每次最多轮换 1 个社区，同一社区当天不重复。
所有直接 Reddit 请求（正式 L1 与影子审计）共享 `config/reddit_access.yaml` 的硬限制：至少间隔
30 分钟、UTC 日总预算 2 次，失败与重试也计数；账号安全锁定期间该入口保持关闭，临时改用
人工触发、只读的浏览器观察。电脑关机造成的自然日空档不丢样本；未满
14 次只显示临时排名，不会自动修改 `config/sources.yaml`。若当天只需重算报告，可加
`--report-only`。

其他日报分区使用同一套影子晋退逻辑，但按栏目采用不同质量指标：发布看官方确认与
模型卡/权重，实测看方法学与复现材料，定价看可读价格正文，降智看诊断与多源印证，
公司动态看公告/财报/监管语义；影子专用的“技术综述/研究解读”则承接有技术深度、
但没有作者本人实施证据的研究解释，避免把高质量二手综合误写成一线实测。配置和入口分别为：

```bash
python scripts/audit_source_partitions.py
```

它同时只读分析正式 `data/raw` 基线、采集 `config/source_audit.yaml` 中的候选，并生成
“关注对象 × 发布/定价/状态/公司材料”覆盖矩阵。候选数据写入 `data/source_audit/`，
报告写入 `reports/source-audit/`，均不进入正式日报。Lilian Weng 等低频原作者使用显式的
扩展内容回看窗，并按 `low_frequency` 节奏单独判断，不会因为日更活跃度低而与新闻聚合器
直接比较。配置为低频轮换的作者不会加入无人值守采样，但可用
`--only trial_jay_alammar` 这类显式专项调用；历史资料库入口即使显式指定也不会自动抓取。

其中 `expert_author` 轨道首批影子采样 10 个来源：Lilian Weng、Sebastian Raschka、
Eugene Yan、Hamel Husain、Shreya Shankar、Armin Ronacher、Max Woolf、
AI as Normal Technology、Jason Liu、Cameron Wolfe。Tim Dettmers、Jay Alammar、
Thorsten Ball 登记为低频轮换；Chip Huyen、Gwern、Colah 只保留历史检索入口。

作者 RSS 的长正文仅在采集进程内用于派生技术主题、方法、第一人称实施、综述和利益关系
命中；落盘快照只保存这些命中、正文长度和哈希，不保存第三方全文。只有明确出现作者本人
运行、构建或评测的文章才进“一线实测”，否则归影子“技术综述/研究解读”。Eugene Yan
与 Anthropic、Jason Liu 与 OpenAI Codex 的现任关系在配置和报告中单列；涉及本公司产品时
标记利益关系，但不自动否定其一线信息价值。

### 规则回放与故事派生

分类规则改动前，可对真实历史快照回放 `HEAD` 版本的分类器与 topics 配置，并和工作区版本比较；输出只比较召回、栏目路由和逐信源变化，不写悬案：

```bash
python scripts/backtest_classification.py --days 14
```

确定性故事聚类保留每条来源观察的 `source_item_id` 与 `snapshot_hash`，并另外生成合并日志。它是可重建的阅读视图，多来源聚在一起不等于独立证实：

```bash
python scripts/build_story_clusters.py --date YYYY-MM-DD
```

聚类之后可继续生成三个私有派生视图：来源传播链与候选独立 origin、30 日信源健康账本、窗口内跨日故事连续性。三者都只帮助阅读和人工复查，不自动改日报、悬案或信源配置：

```bash
python scripts/build_source_independence.py --date YYYY-MM-DD
python scripts/build_source_health.py --date YYYY-MM-DD --days 30
python scripts/build_story_lineage.py --date YYYY-MM-DD --days 30
```

`finalize_daily.py` 会按上述依赖顺序刷新这些视图，再计算备份指纹。

### Windows 定时（Task Scheduler）

```powershell
$action = New-ScheduledTaskAction -Execute "python" -Argument "scripts\fetch_l1.py" -WorkingDirectory "<仓库路径>"
$action2 = New-ScheduledTaskAction -Execute "python" -Argument "scripts\build_digest.py" -WorkingDirectory "<仓库路径>"
$trigger = New-ScheduledTaskTrigger -Daily -At 08:00
Register-ScheduledTask -TaskName "FrontierRadar-L1" -Action $action, $action2 -Trigger $trigger
```

## 设计要点

- **分层证据标注**：厂商口径 / 社区一手 / 聚合 / 聚合指数 / 财经，简报里逐条标明，不混写。
- **悬案账本**（私有 `config/claims.yaml`）：每个未决论断记录证据与状态（open / leaning-yes / leaning-no / resolved），日报自动对照当日条目提示疑似新信号，把一次性调查变成累积性研究。
- **单一事实源**：`claims.yaml` 是唯一权威判断源；自动上下文、月度复盘与 `reports/dossiers/` 都是可重建派生视图。
- **受控写入**：无人值守流程只能通过 `apply_triage.py` 追加已验证的外部可点证据，不能自动新建悬案或改变状态。
- **可审计证据身份**：自动证据逐 URL 保存，明确标注支持/反证/中立/混杂，并绑定抓取条目身份与快照哈希。
- **报告分层**：`reports/dossiers/` 是机械证据档案；`reports/` 根目录的专题裁决报告仍由人工触发的 L2 深挖产生，自动化不会覆盖。
- **统一收尾**：`finalize_daily.py` 在分诊和派生完成后计算产物指纹，镜像并核验私有仓库工作区与 Git 提交树，再用独立同步确认标记闭合最终回执；月度复盘自动选择上一个完整月份。
- **信源发现**：不锁死在固定账号清单。聚合信源的引文作者自动累计（`data/candidates_ledger.json`，私有），达到阈值后提名、人工确认晋升。
- **三条写作纪律**（见 [AGENTS.md](AGENTS.md)）：归因检查、警惕整齐结构、正确优先于连贯。

## 已知信源状态（2026-08-01 验证）

- linux.do 匿名抓取被 403（含浏览器 UA），信源暂关。
- Anthropic 与 Z.ai 无 RSS（404），退为页面快照 + 内容哈希差异观察。
- 港股行情信源为待办（`enabled: false`）。
