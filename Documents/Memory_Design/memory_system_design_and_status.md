# Shovel Memory 系统：当前设计、实现与开发进度

核对日期：2026-11-18。跟踪：[hylinux/shovel#14](https://github.com/hylinux/shovel/issues/14)。
代码基准：`a0fb9ca26e15c8a3c96a93df3a638ef1002bd441`，含近期召回、管理和逐条 UI 修复。

本文是**当前实现快照**，综合 Memory 领域、SQLite/Zvec、Runtime/MAF、
Profile/Working/P3、管理页面、评估代码及既有专题文档。
不是功能重构或新一轮模型评测；没有读取真实用户存储、个人配置或调用付费模型。
既有报告保留原日期和评估范围，设计目标、已实现功能和未完成项分别标注。

## 1. 当前结论

Shovel 已实现本地优先的原生 Memory：
**SQLite 权威存储 + 可重建的独立 Zvec 索引 + 有来源和生命周期的事实 +
持久后台 Operation + 主 Agent 上下文接入 + 人工管理**。

治理、混合召回、事件时间、受管 Profile、结构化 Working 及 P0–P2 修复已接入；
P3 是限定来源的待审经验候选试点，不是自动经验学习。
近期已交付同项目开始/完成双问的逐项证据、显式恢复、分页、删除/清空及逐条操作 UI。

**仍有已确认缺口**：默认原生 Memory 与 Platform 用户命名空间不同，
现有清空谓词不能覆盖正常 Platform 经验候选；Epoch 失效不等于擦除内容。
因此不能把“清空全部长期记忆”描述为已经无遗漏地擦除了所有经验数据，详见 §10。

## 2. 信息分层：不能互相冒充

| 层 | 当前职责 | 明确边界 |
|---|---|---|
| Session History | 原始用户/助手消息、引用和交付状态 | 保存消息不等于自动提炼成跨 Session 事实 |
| Session Summary | 超出历史窗口的增量压缩，保存来源水位 | 不是长期事实，也不是自动事实提取来源 |
| Semantic | 用户明确陈述的偏好、属性、约束、技术选择和决策 | 不将 RAG、助手回答或模型推测当用户事实 |
| Episodic | 有来源的事件；区分明确日期、时间戳和未知时间 | 记录时间不能替代开始、完成等事件时间 |
| Procedural | 人工维护的建议性方法 | 不自动学习，不提供执行权限或修改 instructions |
| User Profile | General 设置资料及受管 Profile Memory 投影 | 资料权威是设置；聊天不能覆盖或恢复已清空字段 |
| Working | 用户显式选择的 Session/task 目标、约束、计划、决策、阻塞和下一步 | 不是 Workflow/Execution 业务状态或副作用检查点 |
| P3 Experience | 从限定持久 Execution 采集的独立观察候选与审核回执 | `reviewed` 仍不激活、索引或进入聊天 |
| Knowledge/RAG | 独立文档/邮件/网页证据及引用 | 与 Memory 的模型配置、索引和来源政策分离 |

Agent instructions 是行为配置，不是记忆。版本化 Skill/Agent 不自动继承主 Agent
的 Memory、RAG 或工作上下文；V1 Skill Memory Access 仍为 Disabled。

## 3. 架构与模块分工

```text
REST / AG-UI / CLI
        |
现有 Generic Host / Runtime / ChatService
        |
        +-- 主 MAF Agent：历史 + 摘要 + 当前 Profile + 选定 Working
        |                 + 有支持证据的长期 Memory + 独立 RAG
        |
        +-- MemoryTaskService：持久 memory.* Operation / Epoch / 重试
        |        |
        |        +-- 隔离、无工具的提取/摘要 MAF Agent
        |        +-- 人工创建/替代/审核/重建/确认清空
        |
        +-- Memory 领域服务 / 仓库与索引端口
                 |
                 +-- SQLite：事实、证据、状态、FTS、Outbox、回执
                 +-- Zvec：独立分区、模型版本、索引代、查询缓存

PlatformApplication --> 独立 P3 ExperienceService / 候选仓库
```

领域对象和提取/索引端口位于 `memory`；SQLite、Zvec、MAF 分别是适配器。
`runtime\memory.py` 装配模型和资源，`memory_tasks.py` 复用现有 Operation worker，
`memory_reset.py` 执行确认清空；不另起 Memory Server 或平行执行引擎。

SQLite 已接入三类事实、`SessionSummary`、`MemoryWorking`、`MemoryExperience`、
索引 Outbox/状态，以及运行时 `MemoryEpoch`。
`MemoryLink`、`MemoryRevision` 等模型的存在，不代表完整关系图或逐次修订审计已实现。
Schema 通过有序、可重复迁移升级；不能仅改 ORM 后假设旧库已更新。

## 4. 归属、Scope 与身份

原生所有者由 workspace 路径派生的内部 ID、用户和 Agent 组成；
默认用户为 `local-user`，默认 Agent 为 `shovel`。

| Scope | 正常召回可见性，同一 workspace/user 为前提 |
|---|---|
| `user` / `workspace` | 当前所有者内可见；`user` 不是跨任意工作区的全局用户库 |
| `agent` | 同一 Agent，跨 Session |
| `session` | 同一 Agent 且同一 Session |

自动用户事实默认写入 `agent` Scope，因此删除来源 Session 不自动删除跨 Session 事实。
管理页 `all_scopes=true` 可管理当前 workspace/user 的其他 Agent/Session 记录，
**不扩大主 Agent 召回权限**，也不是跨用户访问。

规范化 fingerprint 用于同类型、同写入 Scope 的去重和墓碑识别，不是自然语言等价证明。
向量身份复用 `memory_vector_id(kind, id, revision)`；记录被替代时创建新记录，
不以访问次数或追加来源冒充新业务版本。

浏览器恢复使用 `/status` 的公共 workspace 身份，不混用 Memory 内部路径哈希；
二者职责不同，不能因为都叫 workspace ID 就互换。

## 5. 自动写入、整理与时间

1. 正式回答成功时，回答、Working 成功回执及需要的提取/摘要 Operation 在同一事务提交。
   AG-UI `RUN_FINISHED` 确认该持久边界，不代表提取或索引完成。
2. Memory worker 等待前台空闲；只把对应的本轮用户原文交给隔离、无工具的提取 Agent。
   自动候选仅 Semantic/Episodic，最多十条；空候选是正常结果。
3. 程序校验结构、精确原文证据、来源、评分、凭据/引用防线和纠正意图。
   DeepSeek/Qianwen 使用 JSON Object + schema 提示，其余保留相应结构化输出约束；
   任何 provider 均须通过本地验证。
4. SQLite 规范化去重、保守归并、审核/纠正与替代。模型提出 `merge_with` 不能
   绕过主体、谓词、对象/topic 等检查；相关不等于同一事实。
5. 事实、替代关系、索引意图和 Operation 回执原子提交。Zvec 成功写入/删除并 flush
   后才确认 Outbox；索引失败不会把已落 SQLite 的事实伪装成不存在。

`review_required=false` 为默认自动激活；开启后有效候选进入 `candidate`，
批准时重验来源、Epoch、有效期和墓碑。批准不证明事实真实，也不增加执行权限。

事件提取接受证据中完整 ISO 日期或中文数字年月日，转换为日期精度的 UTC Unix 秒；
day 不表示事件确实发生于零点。相对日期和缺失年份不自动补齐。
`explicit`、`recorded`、`legacy_unspecified` 分别表达明确事件时间、
记录时间回退和旧记录语义不明；未知事件时间不能提供日期证据。

## 6. 召回：从相关候选到足以回答

1. 从 SQLite 当前 Profile、独立 FTS5 trigram/短词查询及 Memory 向量读取有界候选。
   关键词保持 OR；ASCII 边界与中英文混排兼容，不依赖通用动词命中证明答案。
2. 向量先过相似度门槛，再回 SQLite 验证归属、Scope、类型、状态、revision、
   模型版本和有效期。向量残留不能复活已遗忘、替代或过期事实。
3. RRF 融合查询相关性；重要性、提取评分、新近性和访问次数仅辅助排序。
   当前 Profile 与历史属性分开，通用资料不能占满查询预算。
4. 有界支持性规则核对实体/别名、主体、关系、事件方向、日期和条件，
   输出 `supported/background/related/conflict/unknown`。
   只有支持事实与背景 Profile 进入长期记忆上下文。
5. 使用紧凑 `context_entry`，保留事实、三元组、时间语义、Scope、ID/revision；
   完整 provenance/evidence 保留在 SQLite 和管理详情。
6. 主 Agent 在总字符预算内再次裁剪；最终 `memory_status` 依据实际保留证据，
   不能只根据召回阶段曾命中过就声称有答案。

**同项目开始/完成复合问**已实现有界拆分：只对完整绑定的同一项目双问逐项判定，
共享候选、去重与预算，不是任意复合句推理。最终 `memory_query` 区分：

- `supported`：最终上下文仍有该项证据；
- `no_match_in_visible_memory`：本次可见候选没有该项支持，不等于全库从未记录；
- `omitted_by_budget`：原有支持候选，但最终预算未保留。

不支持的时间问法另有 `unsupported_query` 状态。
例如只有项目开始日期时，开始可答、完成未知；不得把开始日期或记录创建时间当完成日期。
`sources` 为空仅表示没有本轮知识引用，不能据此否定独立 Memory、Profile 或历史。
内部查询会去除规范化后完全一致的独立问句，重复两遍不应改变事实支持结果；
原始聊天内容、历史和提取来源不改写，时间、否定及真正的个人/团队限定不合并。
除既有的同项目开始/完成双问外，支持至多四个完整、独立且属于现有可识别问法的子问题：
分别提取关键词/向量候选，逐项核验并绑定各自的事实 ID/revision，共享总条数和字符预算。
不同项目不能挪用证据，已知部分保留；优先给每个有证据的子问题一次入选机会，
不能入选的匹配项明确标记预算省略。省略主体、跨句继承目标、同句多项目/多属性、
全局附加条件及更一般的复合问题仍保守处理，不承诺通用自然语言蕴含。

`memory_query` 将 `unsupported_query`、`retrieval_unavailable`、`omitted_by_budget`、
`no_match_in_visible_memory`、`supported` 和 `partial` 分开报告；
旧 `memory_status` 仍保留“本轮是否提供 supported 事实”的兼容语义，不是库存清点。
语义检索失败且关键词回退成功时保留已核验事实，同时标记 `keyword_fallback`；
无支持结果时不得把检索失败解释为无记录。对于明确只查 Memory 的请求，
不支持的问法、无支持证据的检索故障和全部证据被预算省略，由 Host 确定性说明限制，
不让模型据此推翻先前有证据支持的答案；可能还需知识查询的请求则保留正常 Agent 路径并附限制说明。

未配置 Memory embedder 或索引故障时，保留 SQLite 管理、Profile 与关键词召回，
同时显式报告语义不可用；不能把降级当作完整语义召回成功。

## 7. Profile、Working 与摘要

**User Profile**：General 设置采用 revision 检查；文字资料、受管 Memory 投影和
向量意图原子提交，启动/Agent 重启幂等同步。昵称、性别、职业、简介是设置权威，
普通聊天提取不能覆盖或恢复；资料会进入下一轮请求，即使关闭自动 Memory。
头像经净化后独立保存，头像二进制不进入模型上下文。

**Working**：同一 Session 可有多个 task，只有用户明确选择的 active、未过期任务，
且该请求开启 Memory，才进入主 Agent。创建要求修订 0，编辑/删除检查已观察修订；
证据只能引用本 Session 已完成持久化的消息。默认 TTL 七天。
核心字段超预算明确报错，可选计划/引用省略时有 `omitted_fields`。
成功回答才推进聊天引用回执，不自动把任务标为 completed；过期任务不能用旧 ID 复活。
Working 不进入长期向量库，不自动转为事实，也不接管 Execution/Workflow。

**Summary**：历史窗口外消息通过同一后台机制增量压缩，保存水位，可能存在追赶窗口；
关闭本轮长期 Memory 不等于清除 Session History、Summary 或 General Profile。
MAF AgentSession/原生滑动窗口属于模型运行上下文，不替代 SQLite 历史和摘要权威。

## 8. 索引、调度与恢复

Memory 与知识库分开配置 embedding 和维度；只共享 HTTP embedding 适配器等基础设施。
索引按内部 workspace/user 分区，跟踪 active/building generation 及 model version；
Outbox 未排空不能宣称新代已经 ready，旧代目录平时不自动回收。

MemoryEpoch 是遗忘/清空的写入代际围栏，不是向量重建 generation。
提交时重新核对 Epoch、Operation 状态和来源；迟到结果不能越过遗忘边界。
旧请求键保持原意图，不能重绑定到新代事实；未知结果应先查回执，不盲目重发。
失败仅对已分类的暂态错误有界重试；显式 retry 仍检查来源、所有权和代际，
失败的 `memory.purge` 必须重新预览确认，不能通用重放。

前台优先与本地 Ollama 模型准入复用 Runtime，不以知识索引 warmup 全局阻塞提取。
关闭原生 Zvec 句柄、线程 I/O 和索引重建遵守现有资源锁与 Host 生命周期。
正常重开、确定性竞态测试不是杀进程、断电或全部调度交错的证明。

## 9. 管理界面与接口

Settings 的长期记忆管理默认展开，读取当前所有者所有状态/Scope，
每页二十条，服务端过滤后分页；隐藏后停止列表读取。
每项独立卡片、ID、操作、编辑器和确认区，刷新/重排不按列表下标切换目标。
顶部管理区、记忆列表和底部分页有独立视觉边界。

| 操作 | 当前行为 |
|---|---|
| 新建 | 可选择类型/Scope；`POST /api/v1/memories` 返回 202 Operation |
| 编辑 | 只允许 active/deleted/rejected；创建替代记录，`PUT /api/v1/memories/{id}` 返回 202 |
| 类型/范围 | 编辑时 `kind`、完整 Scope 和 `session_id` 不可改变；前端、REST、worker 均限制 |
| 审核 | candidate 可批准，candidate/active 可拒绝；返回持久 Operation |
| 遗忘 | 同步更新 SQLite/任务围栏，向量清理异步，不是 HTTP 202 的提取任务 |
| 彻底删除 | 包括已遗忘记录；明确确认后硬删除该记录及关联 links/revisions，保留向量删除意图 |
| 重建 | 排队 Operation，需要可用 Memory embedder |
| 全部清空 | 先预览和双重页面确认，再提交 `memory.purge`，以最终回执判断成功 |

编辑保持类型/Scope 是设计约束：类型涉及存储和事件时间语义，Scope 涉及可见性。
跨类型转换、跨范围迁移尚无独立确认及原子迁移流程，不能只解锁选择框。
受管 Profile 仍须在 General 编辑/清空；清空字段留下的空值墓碑可彻底删除。

显式编辑可重新记住同 Scope 的已遗忘/拒绝事实，旧匹配墓碑转为 superseded 并链接新事实。
若改成另一事实身份，则保留旧身份的遗忘围栏。自动提取没有该恢复权限；
手动新建命中墓碑仍明确冲突，不能返回“成功但未保存”。

## 10. 删除、全部清空与已确认缺口

| 动作 | 删除/保留边界 |
|---|---|
| 删除 Session | 删除消息/摘要，清理 Working 并软删除 Session Scope 事实；跨 Session Agent 事实不随来源删除 |
| 遗忘/拒绝事实 | 退出召回并排队向量清理，保留墓碑/指纹，防止等价自动提取复活 |
| 单条彻底删除 | 删除指定记录/墓碑；不等于清除全部替代链、源消息或备份；相同事实以后可能重新学习 |
| 全部清空 | 当前原生所有者的事实全状态、摘要、Working、关联历史、索引及旧 Memory Operation 正文；保留最小无正文防重放记录 |

全部清空先生成五分钟、进程内单次票据和统计，页面必须勾选不可撤销提示并输入
`DELETE ALL MEMORY`。服务端校验票据、文字和 Epoch 后才排队；预览不是冻结 ID 清单，
实际执行前新增的同所有者记忆也包含在范围内。取消或无效确认不执行。

执行时阻止活动聊天/Agent 重启，使用 worker/maintenance 锁及 SQLite 写事务，
先移除专属索引分区所有代，再提交事实删除。文件清理失败不提交事实删除、不报成功。
同时清空 General 的昵称、性别、职业、简介并提升 revision，防止投影重建旧文字；
头像保留。成功回执触发浏览器仅清理本工作区的 Memory 请求恢复记录。

不删除其他所有者数据、Session/源消息、附件、知识库、头像、平台 Execution、
外部备份和供应商副本，也不承诺 SQLite/WAL/磁盘的取证级擦除。
保留的源历史在后续新请求中仍可能形成新的上下文。

### 10.1 不能把 P3 失效当作内容擦除

本次代码核对及隔离 SQL 谓词探针确认：

- 原生 owner 默认为 `local-user`；Platform 的 `[platform].user_id` 默认是 `owner`。
- `SQLiteExperienceRepository.capture` 按 Platform Scope 保存经验候选；
  generation 则显式读取绑定的原生 Memory owner。
- `MemoryResetService._owned` 对包括 `MemoryExperience` 的表统一使用原生 owner。
  默认配置下，经验候选的预览计数及删除因此均不匹配。
- 清空后 Epoch 改变可使 candidate/reviewed 投影为 invalidated，**但 data/review_data
  并未因此擦除**；其他状态的候选内容也不能据此视为删除。
- 既有清空回归手工播种同原生 owner 的候选，未覆盖正常 Platform 归属链路。

隔离内存 SQLite 中，一条默认 Platform 归属的合成记录匹配数为 0，
执行现有清空删除谓词后仍剩 1 条。该探针只验证筛选差异，不是完整清空或生产验收。
**这是已确认待修复项，本次仅记录，没有修改清空功能。**
后续应使用 Runtime 已绑定的实际 Platform scope 定义清理范围，
并覆盖正常 `experience.collect` → 审核/拒绝 → 清空及其他所有者保留的端到端测试，
不能为了删除而把其他用户记录无边界纳入。

## 11. P3 经验候选：已实现与未实现

当前只允许人工 root、成功且纯 Input/Condition/Output Workflow 的持久 Execution。
服务端逐字段比较人工 expected 与持久 observed，记录通过或失败观察；
人工期望匹配不证明业务正确，Execution succeeded 也不证明检查通过。

复用 PlatformApplication 的 API/CLI，短事务提交，同来源自然幂等，
检查来源摘要、所有权、修订和绑定 Memory generation。
保留、拒绝、遗忘均为人工动作；Main Agent 无采集/审核工具。
`reviewed` 不生成 Procedural、不索引、不进入主 Agent、不发布能力。
尚无专用前端面板、外部测试/业务回执核验器、自动经验摘要、反馈/激活/回滚闭环。

## 12. 配置默认值与模型分工

| 配置 | 当前默认/语义 |
|---|---|
| `[memory.llm]` | 未配置时复用 `[model]` 配置；提取/摘要有独立客户端与无工具提示 |
| `[memory.embedder]` | 独立于知识 embedding；未配置则没有语义召回 |
| `dim` | 1024；与实际 embedding 维度一致，变更需要重建 |
| `context_max_chars` / `max_items` | 2000 字符 / 10 条；真实模型基线可有显式覆盖，不混写为默认 |
| `min_similarity` / `min_confidence` | 0.55 / 0.8；本轮未修改阈值 |
| `history_messages` / `summary_max_chars` | 20 条完成消息 / 2000 字符，不是二十轮 |
| `review_required` | false；true 时新有效自动候选先待审 |
| `working_context_max_chars` / `working_ttl_seconds` | 3000 字符 / 604800 秒 |

时间均为 UTC Unix 秒，日期精度单独表达；模型预算按字符，不是 token。
运行配置遵循环境变量覆盖 TOML 覆盖默认值；文件专用读取不等于运行时合并配置。
Local-first 不保证模型离线、免费、默认加密或企业合规。
Memory、索引、模型出站、展示、持久化和导出仍是不同用途边界，
人工批准或答案支持性不提供缺失授权。

## 13. 开发进度与证据分层

| 项目 | 当前状态/提交 | 尚不能宣称 |
|---|---|---|
| 基础 Memory、治理、事件/混合召回、Working、受管 Profile | 已接入主线 | 完整自动学习或任意跨用户共享 |
| P0 支持性边界 | `f98fae9` | 通用否定/时态/条件推理 |
| P1 根因与冻结评估 | `35a676c` | 固定语料满分代表未见样本满分 |
| P2 生命周期及诊断 | `30c7807` | 正常 Runtime 重开覆盖断电、杀进程或所有交错 |
| P3 待审经验候选 | `4ad7fa0`，限定来源试点 | reviewed 即激活方法，或自动处理外部回执 |
| 中文项目事件与完整身份绑定 | `0880f80` | 记录时间可作为未知事件时间 |
| 同项目开始/完成逐项支持 | `826a48a` | 任意多实体/复杂复合问均可拆分 |
| 管理页分页、显式恢复、确认删除/清空 | `eee00f4`，关联 #37 | 清空已完整覆盖 Platform 经验候选；见 §10.1 |
| 逐条编辑/确认与卡片分区 | `dce907a` | 未部署的真实 Host 已自动更新 |
| 编辑类型/Scope 限制文档 | `a0fb9ca` | 已实现跨类型或跨范围迁移 |

**历史验证，不冒充本次重新执行：**

- 2026-11-09/10：有版本化提取/召回、冻结 18/27 问、正式自由文本及 Profile/
  生命周期真实模型验证。小规模、诊断驱动的满分不能推导生产正确率；
  首轮端到端召回曾明显低于原始向量候选，须区分提取、候选、准入、最终请求与回答。
- 2026-11-18 管理功能：八文件组合 509 passed、9 live skipped，增补独立
  Profile 墓碑用例后共 510 个不同后端用例；最后修正另有定向复测，不重复相加。
  原生 Zvec、静态导出与隔离 FastAPI/SQLite 浏览器链路另有验证。
- `dce907a` UI：前端全套 132 passed；桌面/移动端、中英文四个浏览器回归，
  验证非首条编辑、拒绝、遗忘、硬删除、取消及重排；lint、类型、静态构建通过。
- 既有全库格式检查有 106 个未改动文件未通过，本轮未重跑或批量格式化；
  不把改动文件检查通过写成所有质量门全绿。

**本次实际验证**只包括源码/配置/文档交叉核对、隔离所有者谓词探针及文档检查；
没有重跑上述功能/真实模型套件，也没有修改/重启真实 Host。
已有 Web 人工验收计划和运行实例版本核验仍需独立完成。

## 14. 后续优先级

1. 修复清空与 Platform 经验候选归属映射，覆盖预览、内容擦除、失效状态和其他所有者保护；
   同步修正文案、范围承诺与端到端回归。
2. 完成最新运行 Host 的 Web 人工验收，包括开始/完成三问、显式更正/恢复、
   单条删除和全部清空；自动浏览器通过不等于用户真实环境手工通过。
3. 扩展未参与规则开发的盲测：未见实体、复合条件、多步更正、长 Session、
   摘要漂移、错误支持/误拒答；固定语料、评估器及来源指纹。
4. 补杀进程、索引中断与 AG-UI 丢流后的持久历史核对，不盲目重发外部副作用。
5. 单独设计经验来源核验、受控方法激活、反馈/回滚及 Memory 开/关任务收益、延迟/费用对照。
   不放宽用户自述准入，也不把记忆变成权限或自动发布通道。

其他限制包括：自然语言改写不具备完美墓碑匹配，单 Session 清理的既有万条枚举上限，
P3 列表/采集有界，旧索引代不日常自动回收，无通用图记忆/主动订阅/任意历史时点查询。

## 15. 文档与代码阅读导航

以下代码链接固定到本次核对提交；文档链接指向仓库主线的既有文件。
本文件作为当前快照，历史实验数字仍以带日期的原始报告说明为准。

| 文档 | 定位 |
|---|---|
| 本文 `memory_system_design_and_status.md` | 当前整体设计、开发状态、已确认缺口和下一步 |
| [目录与历史盘点](https://github.com/hylinux/shovel/blob/main/Documents/Memory_Design/README.md) | 文档导航和 2026-11-09/10 历史评估，不代替当前实现快照 |
| [详细架构](https://github.com/hylinux/shovel/blob/main/Documents/Memory_Design/native_memory_architecture.md) | 存储、身份、Scope、生命周期、模型/索引、事务及 API |
| [完整化路线](https://github.com/hylinux/shovel/blob/main/Documents/Memory_Design/memory_completion_roadmap.md) | 已落地阶段与未来经验/程序性学习路线 |
| [真实模型基线](https://github.com/hylinux/shovel/blob/main/Documents/Memory_Design/memory_baseline.md) | 冻结数据、真实提取/回答、历史指标、运行方法及局限 |
| [P3 经验候选](https://github.com/hylinux/shovel/blob/main/Documents/Memory_Design/experience_candidates.md) | 来源白名单、人工输出检查、候选状态及 API/CLI |
| [向量身份](https://github.com/hylinux/shovel/blob/main/Documents/Memory_Design/db_base_memory_vector_id.md) | UUID、revision、Outbox 与重建 |
| [原始任务](https://github.com/hylinux/shovel/blob/main/Documents/Memory_Design/memory_layer_design.md) / [旧 HTML](https://github.com/hylinux/shovel/blob/main/Documents/Memory_Design/native_memory_architecture.html) | 历史需求/导出，不作为当前已实现能力依据 |

| 代码/验证入口 | 职责 |
|---|---|
| [Memory 领域目录](https://github.com/hylinux/shovel/tree/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/src/shovel/memory) | domain/ports、提取、召回、支持性、SQLite/Zvec、Working/P3 |
| [Runtime 装配](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/src/shovel/runtime/memory.py) / [后台任务](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/src/shovel/runtime/memory_tasks.py) | 资源、配置、Operation、重试与 Epoch |
| [全部清空](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/src/shovel/runtime/memory_reset.py) / [经验仓库](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/src/shovel/db/experience_repository.py) | §10.1 归属差异的直接实现依据 |
| [主 Agent](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/src/shovel/runtime/agent.py) / [ChatService](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/src/shovel/runtime/chat.py) | 实际模型请求、二次预算、持久回答边界 |
| [User Profile](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/src/shovel/runtime/user_profile.py) / [配置](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/src/shovel/config/memory_config.py) | 受管投影与真实默认值 |
| [管理 UI](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/frontend/app/memories.tsx) / [恢复回执](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/frontend/app/request-recovery.ts) | 分页、逐条操作、确认清空及浏览器恢复 |
| [原生集成测试](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/tests/test_native_memory.py) / [生命周期](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/backend/tests/test_memory_lifecycle.py) | 事实/向量/回执与受控中断恢复；注意 §10.1 测试缺口 |
| [评估器](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/scripts/memory_support_evaluation.py) / [浏览器回归](https://github.com/hylinux/shovel/blob/a0fb9ca26e15c8a3c96a93df3a638ef1002bd441/frontend/e2e/memory.spec.mjs) | 分阶段模型证据和桌面/移动逐条操作 |

复验应使用已锁定安装、隔离 SQLite/知识与 Memory 向量/workspace 路径。
普通测试离线；真实模型必须显式 opt-in 并使用获授权配置，可能出站计费。
具体命令见开发环境指南、架构测试章节和基线文档，不把本次文档盘点当成重新跑过测试。
