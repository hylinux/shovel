# Shovel Memory：设计理念与开发进度总览

整理日期：2026-11-09。跟踪入口：[#14](https://github.com/hylinux/shovel/issues/14)。

本文是 Memory 专题文档的阅读入口，区分设计原则、已提交实现、历史验证、
当前开发中改动与未来计划。两轮召回修复基线为已提交并推送的 `ac2a7cc`；
初次盘点时主线 HEAD 为 `2fb040c`，提交本文前已核对新增的
`ca66c7e`（User Profile 与受管 Profile Memory，#21）。
本次文档归档不代表重新运行模型评估，也不意味着已完成生产验收。

## 1. 当前结论

Shovel 已具备**可治理、可追溯的长期用户记忆，以及可恢复的任务工作上下文**。
治理、时间/混合召回和结构化 Working Memory 三个阶段已落地，
并已完成上下文预算、词法命中和答案支持性方面的两轮修复。

但系统还不是自动经验学习闭环：不能仅凭模型回答、调用次数或任务
`completed`，自动学出可信方法、修改行为指令或获得执行权限。
当前真实模型评估是小规模隔离合成基线，不是生产质量保证。

### 文档导航

| 文档 | 定位与阅读顺序 |
|---|---|
| 本文 | 先读：设计理念、能力边界、开发进度与下一步 |
| [原生 Memory 架构](native_memory_architecture.md) | 已接入链路的详细实现、事务、Scope、恢复和配置 |
| [完整化路线](memory_completion_roadmap.md) | 已落地的第 1–3 步与尚未实现的第 4–6 步 |
| [真实模型基线](memory_baseline.md) | 提取/召回诊断、两轮修复、冻结语料与真实回答评估 |
| [向量身份设计](db_base_memory_vector_id.md) | ID/revision、Outbox、失效校验及索引重建 |
| [原始设计任务](memory_layer_design.md) | 历史需求与扩展目标，不是当前功能承诺 |
| [历史架构 HTML](native_memory_architecture.html) | 保留的旧导出，不自动同步；含 6 处原有目录锚点不一致，优先阅读 Markdown |

Memory 专题文件统一放在 `Documents\Memory_Design`。平台、企业安全、
模型配置等跨模块文档仍留在各自分类，不因为提到 Memory 就整体迁入。
文中运行命令均从仓库根目录执行，前端命令另有说明。

## 2. 设计理念

### 2.1 记忆是有证据和生命周期的业务对象

Memory 不是聊天记录的另一个副本，也不是单纯的 `text + embedding`。
一条事实需要回答：谁明确陈述、适用于谁/哪个项目、证据在哪里、
何时有效、是否被纠正/遗忘，以及当前是否允许用于回答。

模型负责提出候选，不直接决定持久身份、Scope、数据库修改和索引删除。
结构化输出、精确原文证据、来源、置信度、状态和事务校验共同决定准入。
`confidence` 是提取评分，不是已经核验的事实正确率。

### 2.2 Local-first，SQLite 权威，向量索引可重建

SQLite 保存事实正文、来源证据、状态、修订、有效期和持久任务。
Zvec 只提供可重建的语义候选；向量命中不能决定权限或事实是否仍有效。
Memory 与知识库使用独立的向量路径和 embedding 配置。

不要求部署独立 Memory Server、Redis、Neo4j 或外部向量数据库。
Local-first 不代表模型一定离线；所选远程 chat/embedding 仍可能出站并计费。
也不能把普通 SQLite/Zvec 当作默认加密或企业合规存储。

### 2.3 保守提取、明确纠正、可解释遗忘

自动提取只读取本轮用户明确自述；助手回答、RAG、工具输出和摘要不自动
成为用户事实。空候选是正常结果，不为“记住”而猜测。
默认激活通过校验的候选；开启 `[memory].review_required` 后先待审。
人工批准表示允许激活，不提高执行权限。

更正保留 supersession 历史；拒绝、删除与遗忘保留必要墓碑和代际围栏。
软删除不是擦除源聊天、WAL、备份、底层介质或模型服务端副本。

### 2.4 候选相关，不等于足以支持回答

向量分数和 RRF 只说明相对相关性。最终回答还需要实体、关系、事件方向、
明确时间及适用条件匹配；“项目用了数据库”不能回答另一个项目的问题。

识别到的冲突不能作为答案证据，缺失/矛盾条件为未知，不从其他项目或
模型猜测补齐。只准入支持事实与独立背景 Profile；没有支持事实时允许
明确说明未找到记录。支持性判断是业务适用性检查，不是授权。

### 2.5 工作上下文不接管业务执行

Working Memory 保存用户维护的目标、约束、计划、决策、阻塞与下一步，
恢复的是上下文，不是外部副作用进度。Execution/Workflow 继续拥有业务状态、
检查点、权限、审批、重试和恢复。记忆中的方法建议不能自动晋升为可执行能力。

## 3. 信息分层与职责

| 层 | 保存/使用什么 | 不应等同于 |
|---|---|---|
| History | Session 原始消息；默认近期窗口为 20 条完成消息，不是 20 轮 | 全部自动进入长期向量库 |
| Session Summary | 窗口之外的会话压缩与来源水位 | 长期用户事实或新的提取来源 |
| Semantic | 明确陈述的偏好、属性、约束、技术选择和决策 | 文档知识或模型推测 |
| Episodic | 重要事件及有证据的日期/时间限定 | 自动完整记录所有经历 |
| Procedural | 显式维护的建议性方法 | 已实现自动程序性学习或执行权 |
| Profile | 通用设置维护的当前资料及受管 Memory 投影；兼容属性沿既有事实读取规则 | 从聊天猜测、覆盖设置或恢复已清空属性 |
| Working | 显式选择的 Session/task 结构化上下文 | Execution 业务状态或隐式跨 Session 任务 |
| RAG | 独立的文档、邮件、网页证据和引用 | 自动成为当前用户的记忆 |

Agent instructions 是行为配置，不是 Memory；工作区自定义 instructions
不会替换隔离提取器/摘要器的提示词。V1 的版本化 Skill Memory 访问仍关闭；
不能假设子 Agent 自动继承主 Agent 的 Memory、RAG 或指令。

## 4. 已落地的运行链路

### 4.1 写入与整理

1. 正式聊天链路成功完成后，在同一事务提交回答、Working 回执及后台
   Memory Operation；`RUN_FINISHED` 确认该持久边界，不代表提取/索引已完成。
2. Host 的 Memory Worker 等待前台空闲，调用隔离、无工具的提取 Agent。
   自动提取只产生 Semantic/Episodic，并通过原文证据、来源等程序校验。
3. SQLite 完成规范化去重、受约束归并、审核状态和显式纠正；事实、
   索引 Outbox 与提取回执原子提交。Zvec 成功写入/删除并 flush 后才确认 Outbox。
4. `MemoryEpoch` 防止遗忘后迟到提取或旧向量复活事实。索引故障不应丢失
   权威事实；重建可产生 embedding 费用。

事件日期区分 day/instant/unknown，不能把记录时间当事件时间。
旧 `legacy_unspecified` 记录不凭空回填日期；相对、含糊时间不自动猜测。

`ca66c7e` 增加独立的 User Profile 设置路径：修订检查的资料保存与受管
Memory 同步，设置字段由通用设置维护。存在该资料后，聊天自动提取不能
覆盖受管字段，相关更正/清空通过设置完成；当前资料优先于历史和摘要，
空值不从旧消息恢复。资料仍是不受信任的数据，不是指令或权限。
头像二进制不进入 Memory 或模型请求。

### 4.2 检索、支持性与上下文预算

1. SQLite 当前 Profile、独立 FTS5 关键词和 Memory 向量提供有界候选。
   关键词保持 OR；ASCII 边界兼容中英文混排，保留中文短词与 Go 等技术词。
2. 向量路径应用相似度门槛，再回 SQLite 复核归属、Scope、类型、状态、
   revision、模型版本及有效期。残留向量不能复活旧事实。
3. RRF 汇总查询相关性；重要性、评分、时间和访问次数仅作次级排序。
   历史 Profile 不能绕过当前属性投影，通用背景不挤占全部查询预算。
4. `memory\relevance.py` 按有界语法检查实体/显式别名、关系、个人/团队、
   主体和事件开始/完成，区分 supported/background/related/conflict/unknown。
5. 只有 supported 与背景 Profile 进入条数/字符预算。领域召回与主 Agent
   共享 `context_entry`，保留三元组、时间限定、Scope、ID/revision 和
   `answer_support`；完整 provenance/evidence 留在 SQLite/管理 API。
6. 主 Agent 还会进行总上下文二次裁剪，以最终留下的事实生成
   `memory_status=supported|no_supported_fact`。缺少记忆支持不否定历史、
   sources 或一般建议，但不能挪用其他实体/关系/日期充当个人记录。

基线保持 `min_similarity=0.55`、领域预算 `context_max_chars=2000`，
没有通过降低阈值或全部过滤改善分数。无 embedding 时仍可管理 SQLite
事实并使用 Profile/关键词，语义不可用必须显式报告，不伪装成完整召回。

### 4.3 Working Memory

一个 Session 可有多个任务；通过 `working_task_id` 显式选择。
支持修订检查、有效期、预算、省略字段标识、聊天来源回执和界面草稿保留。
失败回答不会推进成功回执；成功聊天不自动完成任务。

Session 删除会清理其历史、摘要、Working 和 Session Scope 事实，
不删除独立的 Agent Scope 跨 Session 事实。Working 不进入长期向量索引，
也不自动转成 Semantic。

## 5. 开发进度与交付状态

| 工作项 | 当前状态 | 边界 |
|---|---|---|
| 基础原生 Memory | 已提交 | SQLite/Zvec、Scope、摘要、异步任务、索引维护、遗忘与恢复 |
| 第 1 步：治理 | 已提交 | 显式创建/更正、证据、历史、审核、拒绝墓碑、幂等回执 |
| 第 2 步：时间/混合召回 | 已提交 | 明确时间精度、保守归并、Profile/关键词/向量及失效复核 |
| 第 3 步：结构化 Working | 已提交 | Session/task 隔离、修订/过期、显式注入、回执与 UI |
| 两轮召回修复 | `ac2a7cc`，已推送 | 紧凑投影、查询优先、词边界、支持性准入、无证据回答 |
| 真实模型基线 | 已提交 | 隔离提取/召回、阈值/预算对照、冻结语料真实回答 |
| 设置驱动的 User Profile | `ca66c7e`，已提交（#21） | 修订检查、资料/受管 Memory 同步、当前资料上下文、本地化 UI、头像处理及独立测试入口 |
| 第 4 步：经历到经验候选 | 待开发 | 独立采集可核验 Execution 结果、测试、业务回执与反馈 |
| 第 5 步：程序性学习 | 待开发 | 候选验证、适用条件、激活、反馈、失效和回滚 |
| 第 6 步：受控运营 | 未闭环 | 已有质量基线，但无完整真实任务收益/延迟/成本对照 |

User Profile 在初次盘点时尚未提交，现已通过独立提交交付实现。
本次只核对其提交和代码边界，不修改功能或重新执行该功能评估；
不能用 `ac2a7cc` 的旧测试数量和冻结分数为新 Profile 规则背书。
来源优先级、审核/遗忘兼容性及模型质量仍需按新版本独立回归。

未实现能力还包括图记忆、主动订阅推送、任意历史时点双时态查询、
通用跨用户协作及独立 Memory Server。预留 ORM 不代表功能接通。

## 6. 质量评估：结果与解释

### 6.1 提取与第一轮召回诊断

原始合成基线有 10 条消息、8 个金标准事实、7 个召回问题及 6 个干扰事实，
真实模型重复两轮，提取 Precision/Recall/F1 均为 100%。
部分负例由程序准入过滤，不能全部归功于模型。

原始向量候选 Recall@5 为 100%，但端到端 Hybrid 只有 28.57%；
来源元数据和四条 Profile 占用了约 1770/2000 字符，阈值也有损失。
紧凑投影和排序修复后，端到端 Hybrid 达到 78.57%，预算跳过 52→14。
前后重新提取、措辞可能变化，不能把差值全部归因于单个改动。

### 6.2 冻结 18 问：第二轮支持性修复

`memory-support-v1` 有 9 条正例和 9 条无答案问题，包含原三条误召回、
独立项目、明确别名、跨语言和关系错配。前后使用同一完整 SQLite 记录、
元数据和真实 native 命中列表，不重新提取/嵌入；三类哈希逐项一致。
关键词查询仍执行被测实现。

正式 Main Agent 的实际请求与真实最终回答单独评估，捕获 Host Memory JSON；
工具关闭，要求固定 JSON，以预定对象别名和 ISO 日期评分，不使用模型裁判。
这是受控回答协议，不等于自由文本聊天、持久化或 AG-UI 交付全链路评测。

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| 原始 native 向量候选 Recall@5，阈值前 | 100% | 100% |
| 正例领域上下文 Recall@5 | 77.78% | 100% |
| supported 条目答案支持精确率 | 未标注 | 100% |
| 正例真实最终回答正确率 | 77.78% | 100% |
| 无答案正确拒答率 | 100% | 100% |
| 正例误拒答率 | 22.22% | 0% |
| 无答案问题含任意非 Profile 上下文 | 66.67% | 0% |

修复前已经正确拒答全部无答案问题，因此 **66.67%→0% 是上下文污染
下降，不是最终回答幻觉率下降**。九条正例均保留并正确作答，不是全部过滤。
本轮与上一轮 78.57% 使用不同语料/评估范围，不能串为同一指标的连续提升。
固定小样本没有置信区间、盲测或生产流量，不代表普遍 100% 质量。

### 6.3 工程验证与可重复性

`ac2a7cc` 提交前对字节级一致的独立待提交快照验证：
Memory/Working/后台任务/基线/指令 131 passed、1 live skipped；
Host Memory/预算 17 passed，合计 148 项通过；Ruff 和 mypy 通过，
类型检查覆盖 238 个源文件，锁文件校验和依赖兼容性通过。
更早的共享工作树记录为 239 个源文件，两者范围不同，不混算。

真实模型评估另行显式 opt-in，使用隔离 SQLite/向量/workspace/临时目录，
不读取生产 Session/Memory，不修改用户配置，不公开配置、密钥或个人路径。
锁文件与安装环境的复验步骤见
[开发环境指南](../../prepare-develop-env.md)。

以上是既有验证记录；本次文档整理只检查归档与链接，不重新运行这些测试，
也不把旧的全量测试数量当作当前所有工作区改动已通过的证据。

## 7. 剩余问题与下一阶段优先级

1. **扩展独立质量语料**：未见实体/条件、歧义、多实体、否定、复合问题、
   自由文本答案和多步纠正；分别测候选召回、准入、支持精确率、最终回答、
   正确/错误拒答，保留上下文污染诊断。
2. **提升可核验适用条件**：当前词法/类别/实体语法有界，不是通用蕴含证明，
   仍可能错支持或误拒答。按证据逐步结构化条件；新增持久字段需有序增量
   迁移，保留旧身份与证据，不猜测回填。不能只追逐固定探针。
3. **回归 User Profile 新规则**：设置优先、修订及清空语义已有实现，
   按新版本扩展审核/遗忘兼容性和模型质量评估，避免不同来源互相覆盖
   或复活旧属性；不复用旧基线分数作为新版结论。
4. **再评估阈值或重排**：先证明负例改善且正例不退化；模型重排不能替代
   可核验条件或扩展授权。本阶段没有降低 `0.55`。
5. **经验学习另建来源通道**：只从限定、可核验结果产生待审经验候选，
   不放宽“工具输出不是用户自述”。核验后形成带版本、适用/禁用条件、
   反馈与回滚的方法；确定性执行逻辑进入 Skill/Workflow Draft，
   仍经 validate/publish、权限和审批，不自动发布。
6. **补齐真实任务运营**：Memory 开/关对照、任务结果、纠正成本、延迟、
   token/费用、恢复与导出治理；生产/企业验收与合成测试独立。

企业内容的 READ、DISPLAY、模型提交、Memory、索引、存储和导出分别授权；
人工批准和记忆支持不能代替其中任何授权。
相关跨模块约束见 [平台架构](../platform_architecture.md)、
[企业边界](../enterprise_security_analysis.md)。

## 8. 实现与评估入口

| 入口 | 用途 |
|---|---|
| [memory\domain.py](../../backend/src/shovel/memory/domain.py) / [ports.py](../../backend/src/shovel/memory/ports.py) | 领域对象、来源/Scope/生命周期与端口 |
| [memory\service.py](../../backend/src/shovel/memory/service.py) / [relevance.py](../../backend/src/shovel/memory/relevance.py) | 候选整理、召回、共享投影与支持性判断 |
| [SQLite 仓库](../../backend/src/shovel/memory/sqlite_repository.py) / [Zvec 索引](../../backend/src/shovel/memory/zvec_index.py) | 权威事实、Outbox、向量复核及重建 |
| [runtime\memory.py](../../backend/src/shovel/runtime/memory.py) / [memory_tasks.py](../../backend/src/shovel/runtime/memory_tasks.py) | Host 接入与持久后台任务 |
| [Working](../../backend/src/shovel/memory/working.py) / [主 Agent](../../backend/src/shovel/runtime/agent.py) | 显式工作上下文、最终预算与模型请求 |
| [User Profile](../../backend/src/shovel/runtime/user_profile.py) / [独立回归](../../backend/tests/test_user_profile.py) | 设置资料、受管 Profile Memory 与版本边界 |
| [提取/召回评估](../../scripts/memory_baseline.py) / [冻结回答评估](../../scripts/memory_support_evaluation.py) | 显式 opt-in 的隔离真实服务评估 |
| [Native Memory 测试](../../backend/tests/test_native_memory.py) / [支持性测试](../../backend/tests/test_memory_relevance.py) | 生命周期、索引、边界及支持性回归 |
| [Working 测试](../../backend/tests/test_working_memory.py) / [基线测试](../../backend/tests/test_memory_baseline.py) | 任务上下文、评分、隔离及 opt-in 门禁 |

本地评估报告位于 `.tools`，不应为了发布文档而提交配置、生产存储或个人路径。
目录迁移保留各文档历史结果，历史 Issue 评论不追改；后续引用使用本目录新路径。
