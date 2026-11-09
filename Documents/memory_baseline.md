# Memory 真实模型提取与召回基线

## 范围

这是一套小规模、可重复的真实模型质量基线，不是经验学习、真实业务效果或
企业生产验收。普通测试仍离线；只有显式 opt-in 才会调用所选模型服务，
可能产生费用。

实现入口为 `scripts\memory_baseline.py`，固定数据集为
`backend\tests\fixtures\memory_cases.json`，测试为
`backend\tests\test_memory_baseline.py`。

另有 `scripts\memory_support_evaluation.py`，在逐字冻结的合成事实和真实向量
候选上测量支持性准入及真实 Main Agent 回答；它不重新提取事实。
其范围、运行方法及配对结果见本文最后一节，不与下述提取基线混算。

`memory-baseline-v1` 包含 10 条中英文用户消息、8 个金标准事实、
7 个召回问题和 6 个相近主题的干扰事实。覆盖核心 Profile、饮品偏好、
笔记工具、项目数据库、明确日期的事件，以及问题、猜测、外部引用和模拟
凭据等负例。模拟凭据不是实际密钥。

默认重复两轮，每轮使用独立数据库和索引；重复不是增加独立样本数量。
数据集版本和 SHA256、评估器 SHA256、模型及关键配置、依赖版本均写入报告。
修改案例或匹配规则后，应建立新版本并重新测量，不能为已有结果放宽匹配。

## 三段评估

1. **提取**：调用生产的隔离 MAF 提取器、Memory 应用服务及 SQLite 整理逻辑。
   评分对象是通过程序校验后的记录，不是未经校验的模型 JSON。
   不生成主 Agent 回答，不评估聊天流、后台调度或摘要质量。
2. **金标准召回**：在另一套 Runtime 中人工播种固定事实和干扰项，
   用真实 embedding 建立索引，独立测量召回，避免把提取遗漏算成索引遗漏。
3. **端到端召回**：将实际提取记录与固定干扰项索引化，从新 Session 上下文
   查询，保留模型生成文本、来源元数据和预算对实际召回的影响。

两种召回语料都会走正式索引重建和 ready 检查。索引故障不能用关键词降级
冒充完成语义评估。所有 SQLite、知识向量、Memory 向量、workspace 和临时
文件路径都在评估目录中，扩展关闭，不读取现有 Session 或 Memory。
配置中的模型和 embedding 复用，但不修改配置文件；人工审核在隔离 Runtime
中关闭，以免把“未批准”混淆为提取失败，报告保留原配置和实际值。

## 指标定义

| 指标 | 定义 |
|---|---|
| 提取 Precision / Recall / F1 | 金标准与实际记录一对一匹配后的微平均；多余或重复预测计入 FP，遗漏计入 FN |
| 负例误提取率 | 应无事实的消息中，实际产生记录的消息占比 |
| error_count | 无效结构化模型输出等 `ValueError` 的次数，不能伪装为空候选成功 |
| Recall@5 | 每个问题找回的不同金标准事实数除以该问题的目标事实数，再对问题宏平均 |
| Precision@5 | 找回的不同目标事实数除以 5；返回不足 5 条仍使用分母 5 |
| MRR@5 | 前五条中首个相关事实排名的倒数，未命中为 0，再对问题平均 |

Semantic 匹配检查主体、谓词及预先列出的规范对象/别名；Episodic 匹配检查
类型、明确日期、day 精度和固定事件实体。它不是通用语义等价或真实性判断器，
尤其不能证明事件摘要的每个细节正确。评分不调用额外模型当裁判。

召回报告分为 `keyword_profile`、`vector`、`hybrid`：

- `keyword_profile` 包含生产逻辑中的 Profile 优先级，不是纯 FTS 排序。
- `vector` 是原始向量候选经过当前相似度阈值后的结果；`raw_recall_at_k`
  另记录阈值前的候选召回。该通道不等于最终主 Agent 上下文。
- `hybrid` 走生产应用服务的 Profile、关键词、向量、排序及字符预算。
  报告保留候选标签、原始相似度、返回数量和序列化字符数用于定位损失。

`complete=true` 只表示本轮测量无结构化输出错误，不表示质量达到发布门槛。
认证、网络或索引错误会使运行失败，不能据此宣称质量通过。

## 运行

从仓库根目录运行 CLI，所有评估存储由临时目录托管，结束后清理：

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
try {
  uv run --locked python -m scripts.memory_baseline `
    --config "C:\path\to\settings.toml" `
    --output ".tools\memory-baseline.json" --repeats 2
} finally {
  Remove-Item Env:\SHOVEL_RUN_LIVE_TESTS
}
```

也可以通过 pytest 的同一评估实现运行，显式指定持久报告位置：

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
$env:SHOVEL_LIVE_CONFIG = "C:\path\to\settings.toml"
$env:SHOVEL_MEMORY_BASELINE_REPORT = ".tools\memory-baseline.json"
try {
  uv run --locked pytest backend\tests\test_memory_baseline.py::test_live_memory_extraction_and_recall_baseline -m live --no-cov
} finally {
  Remove-Item Env:\SHOVEL_RUN_LIVE_TESTS
  Remove-Item Env:\SHOVEL_LIVE_CONFIG
  Remove-Item Env:\SHOVEL_MEMORY_BASELINE_REPORT
}
```

配置使用 `AppSettings.load()` 从指定文件读取，不叠加 `SHOVEL_` 环境配置覆盖。
必须提供提取模型和 `[memory.embedder]`；`[memory.llm]` 未配置时复用 `[model]`。
这两个模型可以不同。报告不保存配置内容、密钥、原始 endpoint 或生产路径，
只保留固定合成数据的预测和测量信息。

离线验证评分、存储隔离、真实 SQLite/Zvec 流程和 opt-in 门禁：

```powershell
uv run --locked pytest backend\tests\test_memory_baseline.py --no-cov
uv run --locked ruff check scripts\memory_baseline.py backend\tests\test_memory_baseline.py
uv run --locked ruff format --check scripts\memory_baseline.py backend\tests\test_memory_baseline.py
uv run --locked mypy scripts\memory_baseline.py
```

## 2026-11-09 首次基线

使用 Azure OpenAI `gpt-5.2-chat` 提取、`text-embedding-3-large` embedding，
维度 1024，`min_similarity=0.55`，`context_max_chars=2000`，`max_items=20`。
提取预算 2048 输出 token。MAF core 1.22.0、OpenAI SDK 3.42.1、Zvec 0.8.0。

最终报告：`.tools\memory-baseline-20261109-final.json`（本地生成，不提交配置）。
数据集 SHA256：
`d619b0110dbc84848186581a1f7d49df3c95efc57b755c439f39c826fa808b63`。

两轮都匹配了全部 8 个事实：提取 Precision、Recall、F1 均为 **100%**，
4 个负例均无误提取，结构化输出错误为 0。引用和模拟凭据由程序准入层
直接过滤，不能把这两个负例的正确结果归功于模型。

以下为两轮的宏平均；未调整模型、阈值、匹配规则或预算来提升分数：

| 召回通道 | 金标准 Recall@5 | 端到端 Recall@5 |
|---|---|---|
| 关键词 + Profile | 57.14% | 28.57% |
| 向量原始候选，阈值前 | 100% | 100% |
| 向量，应用 0.55 阈值后 | 35.71% | 42.86% |
| Hybrid，含预算 | 71.43% | 28.57% |

端到端 Hybrid 的 Precision@5 为 11.43%，MRR@5 为 0.2143。
有资格进入提取器的 8 条消息，平均每条约 2.37 秒。
查询先测 vector，再测 hybrid，后者复用查询 embedding 缓存；报告中的
延迟不能当作冷启动或公平的通道性能比较。当前不采集真实 token 使用量、
费用、置信区间或长时间负载。

### 已定位的损失

- **阈值损失**：饮品问题的正确向量候选排第一，但相似度约 0.45，
  低于 0.55，被正式召回规则排除。原始候选 100% 不代表可用召回 100%。
- **预算与 Profile 损失**：自动记录携带来源元数据，四条 Profile 已消耗
  约 1767–1770 字符的 2000 字符预算，相关非 Profile 事实可能放不下。
  人工金标准的来源信息更短，所以相同事实的两段召回分数并不相同。

本次建立基线和诊断，不改生产的阈值、Profile 优先级或预算规则。
下一步应分别做阈值/预算/排序的受控对照，同时检查负例和误召回，
而不是仅凭这一小样本直接降低阈值。

尚未覆盖的质量维度包括长 Session、多步纠正、经验结果学习、真实任务收益、
摘要漂移、最终回答正确性及最终上下文二次裁剪。现有离线生命周期和权限
测试继续承担相应工程契约验证。

## 2026-11-09 召回修复与对照

Issue #14 跟踪本轮诊断、实施计划及验证结果。上述首次基线保留为历史结果，
不能用更新后的评估器哈希重新声称运行了旧版本。

### 实施范围

- 领域召回和主 Agent 共享 `context_entry` 模型投影；完整 provenance/evidence
  保留在 SQLite 和管理 API，以 ID/revision 追溯，不在每个模型条目重复来源
  会话/消息 ID。保留原始事实正文、Scope、source、confidence、revision、
  semantic 三元组、事件时间来源/精度和有效期，不用丢失关系语义换取分数。
- 查询命中的当前 Profile 和其他事实按 RRF 相关性优先；通用 Profile 只补充
  剩余预算。重要性、提取评分、时间和访问次数只作次级排序。条数上限为一时，
  也先有界读取四种 Profile 属性，不让其他更新的属性遮掉查询目标。
- 历史 Profile 不能通过关键词/向量绕过最新属性投影。Scope、状态、有效期、
  revision、模型版本和原有准入规则不变。
- 无关查询暴露英语功能词的子串误匹配：`is` 可以命中 `dislikes`，
  `I` 可以命中多条毫不相关的正文。新增 Memory 专用问句停用词过滤，
  保留 `Go` 等短技术词及中文短词；不修改知识库分词或持久化索引。
- 评估使用 `record_access=false`，避免查询顺序通过访问次数影响排序；
  可记录 Profile/查询候选、阈值过滤、失效命中、预算跳过及序列化字符数。

生产默认 `min_similarity=0.55`、`context_max_chars=2000` 未修改；
`min_confidence` 未降低，用户配置文件未写入。本次不增加自动经验学习或权限。

### 阈值与预算矩阵

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
try {
  uv run --locked python -m scripts.memory_baseline `
    --config "C:\path\to\settings.toml" `
    --output ".tools\memory-recall-comparison.json" --repeats 2 --ablation
} finally {
  Remove-Item Env:\SHOVEL_RUN_LIVE_TESTS
}
```

`--ablation` 在每轮已提取/播种的固定事实和已建立的真实向量索引上，对
`0.35/0.40/0.45/0.50/0.55 × 2000/4000` 做 Hybrid 对照；每个组合不会重新
提取事实，查询 embedding 复用同一索引缓存。变更只作用于隔离 Runtime，
结束或失败均恢复原值。报告新增 Memory 领域召回源码 SHA256。

另有 `memory-unrelated-recall-v1` 五条无答案探针，覆盖鞋码、航空公司、
披萨偏好，以及未记录项目的数据库和评估时间。探针及版本在评估器中固定，
不改变 `memory-baseline-v1` 原始数据和匹配规则。
无关查询误召回率定义为：出现任意非 Profile 事实的探针数 / 探针数，
检查全部返回上下文，不只前五条；常驻 Profile 不计入该指标。
它是保守的上下文误召回诊断，不是最终回答幻觉率。

### 实测结果

最终报告分别为 `.tools\memory-recall-before-20261109.json` 和
`.tools\memory-recall-after-20261109.json`，两份使用相同数据集与评估器哈希，
各重复两轮，模型与 embedding 配置和首次基线相同。
两轮前后提取 F1 均为 100%，错误为 0。以下均为两轮平均：

| 默认阈值 0.55、预算 2000 | 修复前 | 修复后 |
|---|---:|---:|
| 金标准 Hybrid Recall@5 | 71.43% | 71.43% |
| 金标准 Hybrid MRR@5 | 0.3000 | 0.7143 |
| 端到端 Hybrid Recall@5 | 28.57% | **78.57%** |
| 端到端 Hybrid Precision@5 | 11.43% | 21.43% |
| 端到端 Hybrid MRR@5 | 0.2143 | 0.7857 |
| 端到端预算跳过次数，14 次正例查询合计 | 52 | 14 |
| 端到端平均上下文字符数 | 1770.0 | 1477.5 |

各运行的金标准事实固定；前后端到端语料由模型重新提取，措辞与评分可能波动，
并非逐字冻结的配对实验。不能把端到端分数的全部差值都归因于单个改动。
定向离线用例另外验证了 Profile/来源预算导致的遗漏，以及较小条目上限、
历史 Profile、访问频次和最终聊天上下文的事件时间保留。

修复后，2000 字符预算下的阈值对照：

| min_similarity | 金标准 Recall@5 | 端到端 Recall@5 | 无关查询误召回率，两种语料 |
|---|---:|---:|---:|
| 0.35 | 100% | 100% | 60% |
| 0.40 | 100% | 100% | 60% |
| 0.45 | 100% | 100% | 60% |
| 0.50 | 85.71% | 100% | 60% |
| 0.55 | 71.43% | 78.57% | 60% |

**本轮不据此下调生产阈值。** 五条探针仍有三条误召回，说明通用动词和
项目实体/关系不匹配并未解决。原来的端到端 2000 字符预算误召回率为 0，
是四条 Profile 把其他事实全部挡住；不能把“没有任何查询事实能进入”
视为高质量拒答。修复前金标准或扩大到 4000 的端到端探针误召回率为 100%。
停用词过滤消除了鞋码、披萨探针的明显误命中，但还需要更大独立正/负例、
实体/关系相关性验证和最终回答测试，才能校准阈值或引入重排策略。

工程验证：Memory/Working/后台任务/基线测试 **111 passed、1 live skipped**；
Host 总上下文预算定向测试另 **3 passed**；Ruff 检查/格式检查通过，
`mypy backend\src scripts\memory_baseline.py` 检查 237 个源文件通过。
本轮真实模型前后各两轮 CLI 对照完成，未读取生产 Session/Memory。

## 2026-11-09 支持性准入与冻结语料回答评估

### 实施与边界

`memory\relevance.py` 把候选相关性与答案支持分开：

- ASCII 英文词/标识符使用适合中英文混排的词边界，`use` 不命中 `user`，
  `Go` 不命中 `Google/Golang`，但保留 `Go语言`、`使用SQLite数据库` 和
  中文短词。边界条件在 SQLite 限制候选数量之前执行；FTS 保持 OR，
  有界扩展公开类别词，不把全部查询词改为 AND。
- 从明确正文和现有 topic 检查项目等命名实体、显式别名、查询关系、
  个人/团队适用条件和第一人称主体。冲突不能支持回答；条件缺失或
  topic/正文冲突为未知，不从其他项目或模型补齐。英文及中英文混排的
  命名评估、开始/完成关系分别检查；旧 `legacy_unspecified` 时间和
  未知时间精度不能支持日期回答。本轮不新增持久字段、不回填业务条件。
- 融合后内部区分 `supported/background/related/conflict/unknown`，
  只有支持事实与独立背景 Profile 进入预算选择。共享投影携带
  `answer_support`，保留三元组、时间限定与 ID/revision。
  Main Agent 根据最终二次预算后的事实生成
  `memory_status=supported|no_supported_fact`，提示本轮没有记忆证据时
  明确说明未找到记录；不否定历史、sources 或一般建议。

`min_similarity=0.55`、2000 字符领域预算不变；SQLite Scope、状态、
有效期、修订、模型版本及既有权限/出站检查不变。
支持性判断是业务适用性检查，不是授权，不提供执行权。

这是有界规则，不是通用自然语言蕴含证明。公开类别词、实体/别名语法、
主体和关系覆盖有限；未识别表达、未加引号的中文实体、歧义、多实体、
否定与复合问题仍可能产生遗漏或错误。非问句语义检索也不等于已证明
回答正确。不能把未见于小样本的错误解释为不存在；后续应扩展独立
语料和条件结构化，再评估是否需要小规模重排，而非直接放宽阈值。

### 固定语料和运行

`memory-support-v1` 在原有金标准事实/干扰项上增加独立项目、明确别名、
编程语言和事件，包含 **9 条有答案、9 条无答案问题**。
冻结文件为 `backend\tests\fixtures\memory_support_cases.json`。
三条原始误召回及未见项目、跨语言、关系错配均纳入对照；
额外离线用例覆盖混排实体、未知主体、个人/团队及旧时间。
不得修改冻结案例后继续沿用同一配对结果。

第一次运行创建专用、空的合成语料目录，播种固定事实并用真实 embedding
建立 native 索引。随后运行复用同一 SQLite 全记录和每个问题的原始
native 命中列表，包括 ID、kind、revision、similarity；关键词查询仍走
被测实现。禁止对既有目录再次使用 `--create`，未标记或不匹配的目录拒绝复用。

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
try {
  # 仅首次创建使用 --create；使用新的专用目录。
  uv run --locked python -m scripts.memory_support_evaluation `
    --config "C:\path\to\settings.toml" `
    --corpus ".tools\memory-support-corpus-new" `
    --output ".tools\memory-support-first.json" --create
  # 对同一事实、元数据与向量候选再次测量，不重新提取或嵌入。
  uv run --locked python -m scripts.memory_support_evaluation `
    --config "C:\path\to\settings.toml" `
    --corpus ".tools\memory-support-corpus-new" `
    --output ".tools\memory-support-next.json"
} finally {
  Remove-Item Env:\SHOVEL_RUN_LIVE_TESTS
}
```

配置仅复用模型资源；全部存储/workspace/临时路径隔离，不读取生产
Session/Memory，不修改用户配置。评估显式禁止访问计数写入，前后验证
完整记录哈希，防止时间、来源、评分和访问元数据悄然变化。
最终报告记录数据集、记录、原始命中的哈希及六个相关源码文件哈希，
测量期间源码改变会失败。历史 before 报告没有新增源码哈希字段，
不声称前后评估器源码相同。

回答阶段调用实际 `runtime.chat.agent.respond()` 和正式上下文装配、
模型客户端/认证/出站路径，捕获实际发送的 Host Memory JSON；
`tool_choice=none` 禁止工具调用。隔离 workspace 要求输出固定 JSON，
以预先规定的对象别名和 ISO 日期评分，不使用模型裁判。
无答案必须输出 `unknown` 且答案为空。它不是普通自由文本聊天评分，
也不覆盖提取→聊天持久化→AG-UI 交付的完整链路。

### 冻结配对结果

本地报告为 `.tools\memory-support-before-20261109.json` 与
`.tools\memory-support-after-20261109.json`。实际回答模型为
`gpt-5.2-chat`，embedding 为 `text-embedding-3-large`、1024 维。
前后数据集、完整记录及冻结 native 命中哈希逐项一致：

| 哈希 | SHA256 |
|---|---|
| 数据集 | `c96f23de3eeda045a286492c226b02d2115dd3480cf3448c67a51a35cc34390e` |
| 完整记录 | `3bb4603e64ba5950b196f94d70057f25284d4f5be4340e28129b5ff50ea01ab2` |
| native 命中 | `e9ca4859db1340ab99fa56150f1caf353b891d34b91b5ffac229cd0fa2a53139` |

| 指标 | 修复前 | 修复后 |
|---|---:|---:|
| 原始 native 向量候选 Recall@5，阈值前 | 100% | 100% |
| 有答案问题的领域上下文 Recall@5 | 77.78% | **100%** |
| 准入标注为 supported 的事实支持精确率 | 未标注 | **100%** |
| 有答案问题的真实最终回答正确率 | 77.78% | **100%** |
| 无答案问题正确拒答率 | 100% | **100%** |
| 有答案问题误拒答率 | 22.22% | **0%** |
| 无答案问题出现任意非 Profile 上下文 | 66.67% | **0%** |

支持精确率以标注为 supported 的条目为分母，用固定 gold ID 判断是否
属于对应问题的答案证据；没有支持条目时记为 null，不伪装成 100%。
候选召回、领域准入和最终模型回答分别保留，最终请求中的 Memory JSON
可审计。无答案问题修复前已经全部正确拒答，因此 **66.67%→0% 是上下文
污染改善，不是幻觉率下降**。禁止依赖全部过滤取得零误召回；
本次九条有答案问题均保留并正确作答。

这是一轮小规模固定合成样本，不含置信区间、生产流量或独立盲测，
不能作为生产质量保证，也不能替代先前真实提取基线。后续需要更多
未见条件、自然改写、明确错误断言及自由文本最终回答评测。

最终工程验证：Memory/Working/后台任务/基线/指令测试 **131 passed、
1 live skipped**；Host 的 Memory 与上下文预算定向测试 **17 passed**；
Ruff 检查/格式检查通过；`mypy backend\src scripts\memory_support_evaluation.py
scripts\memory_baseline.py` 检查 **239 个源文件通过**。显式 opt-in 的
冻结真实回答评估另行完成，未运行生产认证交互或修改生产数据。
