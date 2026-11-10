# Memory 真实模型提取与召回基线

专题入口：[Memory 设计理念与开发进度](README.md)。
本目录整理保留历史实验结果，不表示重新运行评估。

## 范围

这是一套小规模、可重复的真实模型质量基线，不是经验学习、真实业务效果或
企业生产验收。普通测试仍离线；只有显式 opt-in 才会调用所选模型服务，
可能产生费用。

实现入口为 `scripts\memory_baseline.py`，固定数据集为
`backend\tests\fixtures\memory_cases.json`，测试为
`backend\tests\test_memory_baseline.py`。

另有 `scripts\memory_support_evaluation.py`，在逐字冻结的合成事实和真实向量
候选上测量支持性准入及真实 Main Agent 回答。默认使用金标准事实；
`--create --extract` 可在首次创建时真实提取原始消息，再冻结记录与候选，
后续复用不重新提取或 embedding。
其范围、运行方法及配对结果见下文，不与提取基线混算。

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

## 2026-11-10 P1：实际提取记录的冻结对照

P0 的新提取结果仍有召回损失。保留两轮隔离 SQLite 记录后，逐条定位到两个
可复现的实体条件问题，而非索引缺失或相似度不足：

- `the Cedar project` 这类英文后置名称未被识别；可信事件日期存在，仍无法绑定项目。
- 正文 `project Cedar` 与 topic `project.cedar_database` 被解释成两个项目，
  即使谓词、对象和实际业务实体相同也会保守拒绝。点分隔的 `.database` 原本可用。

修复只在读取时支持英文后置名称，并在正文明确给出唯一同类实体时，消歧已知
topic 属性后缀（如 `_database`、`_language`）。后置形式接受大写首字母专名或
`the <name> project/repository/...`；仍是有界语法，不是通用实体识别。
没有正文实体、不明后缀、正文/topic 实体矛盾时不猜测；真正的
`Harbor_database` 不缩成 `Harbor`。没有重写持久记录、ID/revision 或旧版本哈希。
否定、历史、多实体/关系、事件方向与日期可信度的 P0 限制不变。

### 同记录、同候选、同评估器的真实回答

新增 `memory-support-p1-v1`：27 问，13 正例和 14 个保守拒答问题。
原始 10 条消息调用实际提取器，8 个事实全部一对一匹配（F1 100%，结构化错误 0）；
原有干扰项和新增的 6 个诊断事实人工播种。随后冻结完整记录、元数据和真实向量
候选，不在修复后重新提取或重建向量。本语料用于根因回归，**不是盲测保留集**。

案例文件为 `backend\tests\fixtures\memory_support_p1_cases.json`，与配对报告保持同一版本和字节：

| 案例组 | 数量 | 主要覆盖 |
|---|---:|---|
| 正例 | 13 | 英文后置名称、正文约束的属性后缀、显式别名、跨语言、完整下划线名称、原始提取事实 |
| 保守拒答 | 14 | 未知实体、正文/topic 矛盾、不同关系、否定/历史、多实体/复合关系、错误事件方向 |

离线用 `test_memory_relevance.py` 同时核对旧 18 问、P0 24 问与 P1 27 问；
正式自由文本测试入口保留在 `test_memory_p0_live.py`，其中新增独立 P1 参数组，
不更名旧入口或改写旧案例/历史报告。

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 原始向量候选 Recall@5 | 100% | 100% |
| 领域正例 Recall@5 | 38.46%（5/13） | 100%（13/13） |
| 实际模型请求正例 Recall@5 | 38.46% | 100% |
| 支持事实精确率：领域 / 实际请求 | 100% / 100% | 100% / 100% |
| 真实 JSON 回答正确率：正例 | 38.46% | 100% |
| 正确拒答率 | 100%（14/14） | 100%（14/14） |
| 正例误拒答率 | 61.54% | 0% |
| 反例无关非 Profile 上下文率 | 0% | 0% |

阈值仍为 0.55，领域字符预算仍为 2000。配对报告的十个源码指纹中，
只有 `memory\relevance.py` 不同；提取器、User Profile、上下文服务与评估器未变。
两报告以下哈希逐字相等：

| 冻结对象 | SHA256 |
|---|---|
| 原始消息 + P1 案例字节 | `70a1b255ad20f4079d6d362b8bfcc0cd8317148a29c330362249bef5e9a2fd8f` |
| 完整记录与元数据 | `22c2fbdd1260731bd6fa0c2fcec883882010488e21362197f5749c2f095b419e` |
| 原生候选列表 | `a3a0db6d9d4190ecd6f444ca353f35f4463863af7067c3bab92b390fcdf7335b` |

报告保存每条事实的 ID/revision、keyword 候选、native 相似度、实体解析、
独立支持性标签、领域选择、实际请求选择与最终答案。提取基线另外保留正文、
topic、三元组、事件来源/精度及领域诊断计数，以区分提取、门槛、支持性和预算损失。
JSON 评分改为对象/别名的一对一匹配，避免一个共享别名重复满足不同目标。

本地配对报告为 `.tools\memory-support-p1-before-20261110.json`、
`.tools\memory-support-p1-after-20261110-final.json`；冻结语料目录为
`.tools\memory-support-p1-20261110-corpus`，报告不包含用户生产存储或配置内容。

新环境复现时首次显式创建隔离语料，之后删除 `--create --extract` 复用：

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
uv run --locked python -m scripts.memory_support_evaluation `
  --config "C:\path\to\settings.toml" `
  --cases "backend\tests\fixtures\memory_support_p1_cases.json" `
  --corpus ".tools\memory-support-p1-corpus" `
  --output ".tools\memory-support-p1.json" --create --extract
```

新提取措辞与元数据可变，新创建的语料哈希不保证等于上述已有报告；应比较自己
同一冻结语料上的前后结果。`--extract` 只允许首次创建，不能对已冻结语料补提取。
本对照使用真实 `gpt-5.2-chat` 回答和 1024 维 `text-embedding-3-large` 候选；
不是流式 AG-UI、真实业务收益或企业生产验收。

### 当前版本回归与重新提取

在最终实现上复用 P0 的同一 24 问冻结语料，正例领域/请求召回、真实回答正确率、
支持精确率及正确拒答均为 100%，误拒答与无关上下文为 0。
报告：`.tools\memory-support-p1-p0-regression-20261110-final.json`。
初次服务端返回 HTTP 500，测量直接失败，没有当作拒答或质量通过；随后完整重跑。

新提取的原始基线另跑两轮：每轮 8/8 事实匹配，提取 F1 100%、负例误提取率 0、
结构化错误 0。金标准与实际提取语料的 Hybrid Recall@5 **两轮均为 100%**；
阈值后的纯向量 Recall@5 仍分别为 35.71% / 50%，说明关键词/实体通道仍有作用，
并非所有通道都满分。报告：`.tools\memory-p1-extraction-20261110-final.json`。
P0 的 85.71% / 71.43% 保留为历史；两批重新提取不是冻结措辞的因果对照，
应以上述同记录、同候选的 P1 实验归因，而非把新提取差值全部归因于规则。

当前版本的 4 个 Profile live 测试通过，覆盖 Memory 开/关下的
设置、更新、重启、清空和不可信 bio；报告位于
`.tools\memory-p1-profile-20261110-final`。它们不代替 P2 的全部跨组件组合验收。

自由文本采用正式 ChatService、真实向量与实际请求捕获，不强制 JSON。
评分接受等价的明确英文/中文事件日期，以及明确拒答或澄清；条件性举例不等于
断言用户事实。保留了初期失败报告：分别暴露示例数据库被误判为断言、
`August 21, 2026` 与 ISO 同日期被误判，以及澄清请求未被识别的问题，
均补上离线评分回归，而非把它们算作 Memory 召回失败。
此有限规则仍需人工核对，不是通用蕴含评分器；一般知识建议的正确性、
所有措辞的幻觉率和 AG-UI 流式持久交付不在本轮质量声明中。

最终 **16 个自由文本用例通过**（8 P0 + 8 P1），报告分别位于
`.tools\memory-p1-p0-freetext-20261110-complete`、
`.tools\memory-p1-freetext-20261110-complete`。逐份核对实际请求、检查结果、
源码与测试文件哈希；加上单独已通过的 4 个 Profile 和 1 个两轮提取测试，
本轮共有 21 个不同 live 用例通过，不重复累加重跑。

实现期间 Memory/Profile/Working/后台任务/Host/指令组合回归
310 passed、21 live skipped；最终解析与评估定向回归 99 passed、1 live skipped，
两者重叠，不相加。最终变更文件 Ruff 检查/格式检查通过，
`mypy backend\src scripts\memory_baseline.py scripts\memory_support_evaluation.py`
检查 242 个源文件通过；`uv lock --check`、`uv pip check` 通过。
配置文件运行前后哈希不变，生产存储未参与测量。

## 2026-11-10 P0 真实模型补验

在 `92f55bb` 加本次工作区 P0 实现上，显式 opt-in 复用已授权的现有配置，
使用 `gpt-5.2-chat` 和 `text-embedding-3-large`（1024 维）。
全部 SQLite、向量、workspace、临时文件与 Session 隔离；不读取生产记忆，
不更改配置。运行前后配置文件哈希一致，报告不包含配置内容、密钥或个人路径。
阈值 `0.55`、领域预算 `2000` 不变。

### 冻结候选与真实最终回答

保留原始 `memory-support-v1` 文件与历史报告不变，使用新目录建立当前版本
18 问基线；另加 `memory-support-p0-v1`（24 问）独立覆盖否定、历史时态/年份、
并列实体、复合关系、事件开始/完成，以及正例、显式别名、跨语言和 dislikes。
P0 中 16 个保守拒答案例包含当前不支持逐子句拼接的多目标查询；这表示当前
准入能力有限，不表示数据库中绝对没有任何相关子事实。

| 当前版本指标 | 原 18 问：9 正/9 无答案 | P0 24 问：8 正/16 保守拒答 |
|---|---:|---:|
| 原始 native 候选 Recall@5 | 100% | 100% |
| supported 条目支持精确率 | 100% | 100% |
| 正例上下文 Recall@5 | 100% | 100% |
| 正例真实最终回答正确率 | 100% | 100% |
| 正确拒答率 | 100% | 100% |
| 正例误拒答率 | 0% | 0% |
| 无答案问题非 Profile 上下文率 | 0% | 0% |

报告保存候选列表、准入诊断、实际 Host Memory JSON、真实答案与源码指纹，
分别为 `.tools\memory-support-current-20261110.json`、
`.tools\memory-support-p0-20261110.json`；各自冻结语料目录也保留。
本次不是与旧实现逐字冻结配对的提升实验，不能据此计算改进幅度。

复现 P0 测量（首次需使用新的空目录，后续去掉 `--create` 复用）：

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
try {
  uv run --locked python -m scripts.memory_support_evaluation `
    --config "C:\path\to\settings.toml" `
    --cases "backend\tests\fixtures\memory_support_p0_cases.json" `
    --corpus ".tools\memory-support-p0-new-corpus" `
    --output ".tools\memory-support-p0-new.json" --create
} finally {
  Remove-Item Env:\SHOVEL_RUN_LIVE_TESTS
}
```

`--cases` 不改变默认旧语料；案例字节仍参与 manifest 哈希，不能换案例后复用
同一冻结目录。离线测试检查两套 gold 支持性及自定义案例仍受 live opt-in 限制。

### 自由文本、持久聊天与 Profile

新增 `backend\tests\test_memory_p0_live.py`：通过正式 `ChatService.send`，
使用真实模型、正常自然语言回答和隔离 Session；工具调用关闭，不强制 JSON。
8 个案例（3 正例、5 保守拒答）全部通过，检查真实最终回答以及实际请求中的
`memory_status`、supported 条目，覆盖正例、别名、日期、否定、历史、
多实体、复合关系及事件方向。没有 Memory 条目时也读取参考数据消息中的状态。

首轮采集器只找 Host 消息，导致 5 个无 Memory 场景采集失败，而非模型错误；
修复采集后完整重跑 8 个案例通过。自由文本使用确定性值/拒答语言检查并人工
复核保存的全部答案，不使用模型裁判，不把这种有限评分当作通用蕴含证明。
初轮回答中出现“可提供资料后识别 SQLite 等产品”的一般示例，不属于项目事实
断言，不能仅按出现产品名就统计为幻觉。此类评测仍不覆盖 AG-UI 网络交付。

现有 `test_user_profile_live.py` 4 个测试也通过：Memory 开/关两种情况下，
初始资料、修改、Agent 重启、清空共 8 次回答，以及 2 次不可信 bio 指令测试。
全部实际模型回答保持当前资料、不恢复旧值，也未执行 bio 中的注入指令。
最终完成 **13 个 live pytest 用例**（8 P0 + 4 Profile + 1 提取基线）。

```powershell
$env:SHOVEL_RUN_LIVE_TESTS = "1"
$env:SHOVEL_LIVE_CONFIG = "C:\path\to\settings.toml"
try {
  uv run --locked pytest backend\tests\test_memory_p0_live.py `
    backend\tests\test_user_profile_live.py backend\tests\test_memory_baseline.py -m live --no-cov
} finally {
  Remove-Item Env:\SHOVEL_RUN_LIVE_TESTS
  Remove-Item Env:\SHOVEL_LIVE_CONFIG
}
```

保留最终报告 `.tools\memory-p0-free-text-20261110-verified\`、
`.tools\memory-profile-live-20261110\`；首轮采集失败报告独立保留，不冒充通过。

### 重新提取基线：仍有召回损失

独立运行原有 `memory-baseline-v1` 两轮，报告
`.tools\memory-extraction-current-20261110.json`，不是上面固定记录的回答评分。

| 指标 | 第 1 轮 | 第 2 轮 |
|---|---:|---:|
| 提取 Precision / Recall / F1 | 100% | 100% |
| 结构化输出错误 | 0 | 0 |
| 固定金标准 Hybrid Recall@5 | 100% | 100% |
| 重新提取后 Hybrid Recall@5 | 85.71% | 71.43% |

端到端均值为 **78.57%**，不能写成“P0 后整体 Recall 100%”。评分能匹配
三元组/日期，不证明正文/topic 满足所有查询适用条件。例如本轮提取事件正文为
`User completed the offline evaluation for the Cedar project.`，与当前有界实体
语法不同；另有数据库事实未准入，需要后续用完整条件诊断继续定位。
这些运行重新提取了语料，不与历史报告作逐字冻结的因果对照。
本轮只完成测量与记录，没有靠放宽条件、下降阈值或删掉失败问题改善分数。

补验代码最终离线检查：**70 passed、9 live skipped**（该离线命令未设置 opt-in，
对应真实用例已另行执行，不是遗漏）；Ruff 检查/格式检查通过，
`mypy backend\src scripts\memory_baseline.py scripts\memory_support_evaluation.py`
检查 **242 个源文件通过**。另按每个报告逐项验证实际模型请求中的 supported ID
恰好等于 gold、状态正确，并核对当前源码/测试哈希，没有以仅看汇总分数代替核验。
