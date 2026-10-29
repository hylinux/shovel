# Shovel 扫描管线

> 对应代码: `exceptions/pipeline.py`、`pipeline/parsers/*`、`pipeline/chunker.py`、
> `pipeline/embedder.py`、`pipeline/sink.py`、`pipeline/scan_service.py`、
> `cli/commands/scan.py`

资源模块解决的是"怎么连上一个数据源"。扫描管线解决的是紧接着的那个问题:
**连上了之后, 怎么把里面的东西变成可以被检索的块。**

`Connector.discover()` 的消费者是本模块, CLI 与 REST 统一由 Host 调用它。

## 持久后台执行与检索

`POST /api/v1/resources/{id}/scans` 在同一个事务中保存排队 Job 和完整
Operation 参数, 立刻返回 202。后台服务复用该 Job ID 执行, 保存发现进度、
逐文档计数与事件。取消会在发现批次和文档边界生效; 中断的完整发现不会
触发删除对账。Host 停止造成的中断与用户主动取消分开处理, 重启可恢复
前者。已结束 Job 在操作凭据写入前崩溃时, 从 Job 重建结果而不是重新扫描。
重放同一 Job 时会清除该 Job 上次尝试留下的观察标记, 重新建立本轮发现集合;
否则两次尝试之间被删除的文件会因旧标记仍等于 Job ID 而漏掉墓碑对账。

Web 和 `shovel scan run/cleanup` 使用同一执行链。只读的 list/show 直接
查 SQLite。运行 Host 时 CLI 走本机 REST, 离线时使用短生命周期 Host。
`--no-embed` 仍写入可关键词检索的块, 不打开 Zvec; 后续普通向量扫描补齐
索引。只有向量工作需要已配置 embedding 和已初始化的 Zvec collection。

Host 启动时执行幂等的增量 schema 迁移, 建立并回填 SQLite FTS5 trigram
索引, 块变更由触发器同步。中文短查询使用转义、参数化的包含查询。
语义检索使用 Zvec 的资源/模型/粒度预过滤, hybrid 用 RRF 合并排名。
SQLite 再检查资源可读状态、有效文档版本和向量元数据, 并展开到父块供
阅读和对话引用, 不把向量库中孤立/过期的文本直接当成有效知识。

---

## 1. 一条链路

```text
discover  →  fetch  →  parse  →  chunk  →  embed  →  persist  →  reconcile
连接器       连接器     解析器    切块器    向量化     SQLite      墓碑对账
```

每一段之间只用纯数据结构通信, 不共享对象:

```text
DiscoveredItem   连接器给出的"有这么个东西", 只有元数据, 没有内容
FetchedBlob      字节流 + content_type, 还不认识格式
ParsedDocument   带结构的块序列 (Block), 认识格式但还没切
Chunk            切好的文本片段, 有 id 有边界, 还没有向量
VectorRecord     文本 + 向量 + 标量字段, 准备进 Zvec
```

这条边界的收益很直接: 每一层都能单独测。`test_parsers.py` 不需要数据库,
`test_chunker.py` 不需要文件, `test_vector_sink.py` 不需要 embedding 服务。

---

## 2. 解析: 为什么产物不是一根字符串

最省事的做法是每个解析器返回 `str`, 后面统一按长度切。这个做法会丢掉三样
后面再也找不回来的东西:

- **块边界。** 一个表格、一段代码、一页幻灯片, 在纯文本里和散文长得一模一样。
  切块时就只能凭长度下刀, 于是一半的表格落在这个块, 另一半落在下一个块。
- **结构路径。** "这段话在第 3 章第 2 节下面"是很强的检索信号, 拼成字符串以后
  就退化成了普通文字。
- **定位信息。** 用户点开一条检索结果, 期望看到的是 `p.7` / `Sheet1!A1:D20` /
  `slide 5`, 而不是"字符偏移 14823"。

所以解析产物是 `ParsedDocument(blocks=[Block, ...])`, 每个 `Block` 自带
`kind` (`BlockKind`: 散文 / 标题 / 表格 / 代码 / 列表 ...)、结构路径和定位串。

### 路由按扩展名, 不按 doc_class

注册表用扩展名挑解析器, 而不是用资源上配的 `doc_class`。原因是 `doc_class`
里的 `OFFICE_DOC` 一个值同时覆盖 docx / xlsx / pptx —— 三种完全不同的解析方式。
扩展名是文件自己带的, `doc_class` 是人填的, 前者更难填错。

### 当前支持的六种

| 扩展名 | 解析器 | 结构来自 |
| --- | --- | --- |
| `.txt` 及纯文本 | `plain_text` | 空行分段 |
| `.md` / `.markdown` | `markdown_doc` | 标题层级、代码围栏、表格 |
| `.pdf` | `pdf_doc` | 页 (`p.N`) |
| `.docx` | `word_doc` | 标题样式、表格 |
| `.xlsx` | `excel_doc` | 工作表 + 单元格区域 |
| `.pptx` | `powerpoint_doc` | 幻灯片 + 备注 |

不认识的扩展名抛 `UnsupportedDocumentError`。它被**单独对待**: 文档标
`SKIPPED` 且**不计入 `failed_docs`**。"这个目录里有 200 张图片"不是错误,
把它算成失败会让每一次扫描都以黄色告警收尾, 于是告警很快就没人看了。

### 空文本层的 PDF

扫描件 PDF 不做 OCR；没有可用正文时会明确记录解析/无内容失败，
不会把一个没有块的文档当作已成功索引。

---

## 3. 切块: 两层, 用字符数

### 小块进向量库, 大块留在 SQLite

```text
PARENT   ~3200 字符   不做 embedding   只存 SQLite, 供阅读与拼上下文
CHUNK    ~800 字符    做 embedding     进 Zvec, 负责召回
```

这是 small-to-big: 召回要小块(语义集中, 向量才准), 阅读要大块(有前后文,
模型才看得懂)。两个诉求互相打架, 所以干脆各存各的。父块不进向量库, 因为
把同一段内容 embedding 两遍, 换来的只有一份多出来的存储和一堆重复召回。

### 预算单位是字符, 不是 token

token 化是绑定模型的。用 token 做预算, 换一次 embedding 模型就会让全库每一个
块的边界都变一遍, 也就等于全库重新 embedding。字符数是模型无关的, 代价是要
对语言做个粗估: `CJK_RATIO=0.7` / `LATIN_RATIO=0.25`。估得不精确没关系 ——
块边界只要**稳定**就行, 精确是模型那头的事。

### 原子块不许被合并

碎块合并(`_merge_tiny`)存在的理由是避免产出一堆只有十几个字的块。但它必须
**禁止跨原子边界合并**: 把一段散文并进代码块, 或者把代码块尾巴并进下一段散文,
都会让那个块既不是代码也不是散文。两个方向都堵死。

原子块只有超过上限时才会被切开 —— 那时候是"不得不", 不是"顺手"。

### `CHUNKER_VERSION` 参与 id 计算

`chunk_vector_id(doc_uri, content_hash, chunker_version, granularity, index, resource_id=...)`。

扫描会比较已存的切块版本, 因此 **改 `CHUNKER_VERSION` = 下次扫描重切 + 重 embedding**。这是故意的:
切块逻辑变了而 id 不变, 会让旧向量和新文本在 Zvec 里共存, 而且没有任何办法
发现这件事。

当前版本为 `2`: 扫描产出的 id 加入资源身份, 同一文件被两个资源收录时不再
争用 SQLite 主键或覆盖向量。旧调用不传 `resource_id` 时仍保留原计算规则;
已有文档通过版本比较在下次扫描时逐篇重建, 不修改数据库 schema。

### `context_prefix` 不进 SQLite 正文

同一个块有两份文本, 分别服务于两个不同的目的:

| 存在哪 | 字段 | 内容 | 给谁用 |
| --- | --- | --- | --- |
| SQLite | `Chunk.text_` | 纯原文 | 展示给用户、拼上下文给模型 |
| Zvec | `text` | 结构路径 + 原文 (`embed_text`) | embedding 与全文检索 |

前缀能让"第 3 章 / 部署 / 这一节说……"比裸文本更容易被命中, 所以 embedding
和 FTS 都该看到它。但它是**系统加的**, 不是用户写的 —— 出现在展示正文里就成了
莫名其妙的重复标题, 所以 SQLite 那一份必须干净。

---

## 4. 向量化

`Embedder` 协议只有 `embed()` / `model_version` / `dimension` / `aclose`。
实现有两个: `HttpEmbedder` (OpenAI 兼容端点) 与 `NullEmbedder`。

### `--no-embed` 不是调试开关

embedding 是整条链路上**唯一**需要外部服务的一步。没有它, "我先把本地笔记
索引起来看看"就变成一件必须先注册账号、先填 API Key 的事。

关掉之后 chunk 照常写进 SQLite, 停在 `vector_state=pending`, 文档停在
`pipe_state=chunked`; 不提前确认 `indexed_hash`。等 embedding 配好了再运行
普通扫描(包括默认的增量扫描)即可重读、切块并补齐向量。

### 响应必须按 `index` 重排

OpenAI 的 embedding 接口**不保证**返回顺序与请求顺序一致。不排序的话不会报错,
只会让 A 的文本配上 B 的向量 —— 一个纯静默、只能靠"检索结果莫名其妙"发现的错误。

### 期望维度在装配时校验

`build_embedder(..., expected_dimension=settings.zvec.dim)`。两边不一致的后果
同样是延迟暴露的: 向量写进去了, 但检索永远不对。宁可在开始扫描前就炸。

---

## 5. 落库

### Zvec 那一侧的几个坑

都是只有真跑一次才会撞上的:

- `collection.delete(ids)` 是**按 id 删**。按条件删是
  `collection.delete_by_filter(expr)`。传错了不报错, 只是什么都没删掉。
- 过滤表达式的比较符是**单个 `=`** (SQL 风格), 写 `==` 会被语法分析拒掉。
- `upsert` 吃 `list[Doc]` 而非 `list[dict]`。`Doc(id, vectors, fields)` ——
  向量走 `vectors`, 标量走 `fields`, 混着放不报错但字段不进索引。
- 失败放在返回的 `Status` 里, **不抛异常**。所以有 `_ensure_ok()`。
- 写完要 `flush()`, 否则可能没落盘。

### 先删旧块, 再写新块

一篇文档重新解析后块数可能变少。不先删就会留下"幽灵内容": 上一版的第 7 块
还在库里, 而这一版只有 5 块 —— 检索得到, 但文档里已经没有这段话了。

### 向量库也要显式删, upsert 覆盖不了

SQLite 那侧删干净了还不够。chunk id 里含 `content_hash`, 所以内容一变,
**所有**块的 id 都跟着变, 新 id 不会撞上任何旧 id —— upsert 只会在旁边
多写一份, 旧的那批永远留在 Zvec。

于是两边对不上: 检索命中一个 SQLite 里根本不存在的 chunk。所以写新向量
之前必须先 `delete_document(resource_id, doc_uri)` 把这篇的旧向量整篇撤掉。

这个缺陷用假 embedder 测不出来 —— 只有拿真实模型跑完整链路, 再拿 SQLite
的 id 集合去比对 Zvec, 才会看到多出来的那些。

### 完成标记必须晚于向量落盘

现在以一篇文档为提交单元, 不再跨文档缓冲向量:

```text
SQLite 事务: 替换 chunk + 文档标 CHUNKED + 写删除意图
  → 执行删除意图并 flush, 成功后确认该意图
  → upsert 本篇新向量并 flush
  → SQLite 事务: chunk 标 INDEXED + 文档标 INDEXED + 确认 indexed_hash
```

任何一步中断, 都不会提前确认文档完成。包括向量已经落盘但最后一个 SQLite
事务尚未提交的情况, 重跑也会幂等地重建。本篇 embedding 仍由 HTTP 层分批发送。

旧数据中“文档已 INDEXED, 但 chunk 仍 PENDING”的状态也会进入恢复队列。
这保证可恢复, 不保证无间隙切换: 删旧与写新之间, 本篇可能暂时无法被召回。

### 删除使用已有 DeletionOutbox

重索引、墓碑对账和资源删除, 都把删除意图与元数据变更放在同一 SQLite 事务中。
`IndexCleanupService` 按顺序执行幂等删除, 只有向量库确认并 flush 后才写
`done_at`; 失败保留 `attempts` / `last_error`, 中止并显式报错。

带 embedding 的扫描会先重试待处理清理, 资源删除 CLI 也会立即尝试清理。
没有 embedding 配置时可单独运行 `shovel scan cleanup`。
`--no-embed` 不打开向量库, 所以只保存删除意图, 不假装向量已撤下。

---

## 6. 增量与对账

### 判据是 hash, 不是 mtime

内容指纹、流水线状态、切块版本、embedding 模型版本和待补向量共同决定是否
重新处理, 不是只看 `indexed_hash != content_hash`。秒级 `modified_at` 不是可靠游标：
同秒修改会被抹平，复制的新文件也可能保留较旧时间。当前本地元数据指纹包含纳秒
mtime，所以 `touch` 会保守地触发重处理；这不是密码学内容哈希。

(`local_fs` 的 `content_hash` 本身是 `st:{size}-{mtime_ns}`, 见资源模块文档 ——
但那是连接器内部的选择, 管线这一层只认"这个串变了没有"。)

本地文件系统不使用修改时间水位过滤，始终枚举元数据，再按上述条件挑选文档：
同秒修改、复制时保留旧时间戳的新文件都不能被漏掉。其他连接器只有在可靠的
增量游标契约下才能使用水位；发现待恢复或处理配置变化时仍撤掉游标限制。
水位和恢复检查按 Profile 的成员范围计算，不能借用另一 Profile 的进度。

文件未变但向量集合被外部清空时，用 `--force` 强制重建。parser 版本、解析上限、
chunker 默认值和 Profile pipeline 已纳入持久指纹，变更会自动重建。
Embedding 身份包含端点、API 版本、维度、输入上限、显式 revision，不含密钥。
恢复不会绕过资源的 include/exclude、大小及时间窗口限制。

### 删除对账依赖完整的 discover

`Job.is_discovery_complete` 为假，或连接器没有 `DELETE_DETECT` 时**不做**删除对账。
根目录不存在/变为普通文件、walk/stat 权限失败、根目录身份变化会让发现明确失败，
而不是被当成成功枚举到零个条目。原有索引不因这轮失败而被墓碑化。

观察集存于 `profile_document`，重放 Job 时清除该 Profile 上次尝试的观察标记。
全量对账只移除当前 Profile 未再发现的成员；只有没有其他 Profile 成员关系的
文档才退役。Profile 共享同一资源/文档的正文及索引，重叠范围的 pipeline 必须相同，
不支持以同一文档身份保留多套独立处理结果。

理由是删除判据只能是"这一轮没见到它"。如果枚举中途挂了, 或者被 `--limit`
截断了, "没见到"就不等于"不存在了" —— 一次失败的扫描会把整个资源集体立碑。

所以 `--limit` 只截断"本轮处理多少篇", **不截断 discover**。看起来浪费,
但这是唯一能同时支持"只处理 10 篇"和"敢做对账"的做法。

### 立碑, 不删除

对账把 `last_seen_run != job_id` 的都标成墓碑。

不物理删除, 是因为文件"消失"在真实世界里经常只是临时的 —— 网盘没同步完、
U 盘没插、目录被临时改名。立碑的语义是"这一轮没看见", 下一轮见到了就自动复活,
不需要任何人工干预。

### 立碑要同时撤向量, 并把 `pipe_state` 打回 `PENDING`

`lifecycle_state` 只是 SQLite 里的一个标记, Zvec 并不知道它。光改状态的话,
一篇已经从源端消失的文档照样能被检索命中 —— 而立碑对用户的承诺恰恰是它
不该再出现在结果里。所以立碑要连带 `delete_document`。

撤了向量就必须同时把 `pipe_state` 打回 `PENDING`, 否则复活那一步会出问题:

```python
# Document.needs_processing
if lifecycle_state is not ACTIVE:
    return False
if pipe_state in (PENDING, FAILED):
    return True
return indexed_hash != content_hash
```

文件原样搬回来时内容哈希没变, 如果 `pipe_state` 还停在 `INDEXED`,
`needs_processing` 会返回 `False` —— 文档回到 `ACTIVE` 了, 向量却已经被撤掉
且永远不会重建。所以对账那条 UPDATE 同时写三样: `lifecycle_state`、
`pipe_state`、以及 `Chunk.vector_state`。

因为要逐篇调 sink 删向量, 对账拿不到一条纯 UPDATE 就完事的便利 —— 得先
`SELECT` 出这一轮没见到的 uri 列表, 计数也改用 `len(doomed)` 而不是
`rowcount`。

---

## 7. 编排层的几处顺序敏感代码

`ScanService.scan()` 的骨架:

```text
_preflight()   解析资源, 确认状态可扫, 建 Job
_discover()    跑完整个 discover, 落成观测集
_process()     逐篇 fetch → parse → chunk
_persist()     先删旧 chunk, 再写新 chunk
_flush()       本篇向量落盘后再确认文档完成
_reconcile()   墓碑对账 (仅在 discover 完整时)
_close_job()   结算状态与统计
```

### 解析是同步的, 要挪出事件循环

pypdf / python-docx / openpyxl / python-pptx 全是同步库, 而且是 CPU 密集的。
直接在协程里调用会把整个事件循环钉住, 于是心跳停了、并发的其他资源也停了。
统一用 `asyncio.to_thread` 挪走。

### 单篇失败不中断整轮

一篇文档解析失败, 记事件 + 计入 `failed_docs` + 继续下一篇。整轮以
`COMPLETED_WITH_ERRORS` 收尾。

理由是扫描的输入是"用户硬盘上的真实文件", 里面一定有损坏的 PDF、加密的 docx、
以 `.txt` 结尾的二进制。一篇坏文件让两万篇的扫描全军覆没, 这个管线就没法用。

### 心跳

`_HEARTBEAT_SECONDS = 30`。长时间扫描不写心跳的话, 从外面看不出"还在跑"
和"进程已经死了"的区别。

---

## 8. CLI

```bash
shovel scan run <资源> [--mode] [--profile] [--target] [--limit] [--no-embed] [--force]
shovel scan list [--resource] [--limit]
shovel scan show <作业 id> [--events N]
shovel scan cleanup [--resource-id <id>]
```

`cleanup` 不要求资源仍然存在, 因此可以重试已删除资源的清理。
资源移除后 CLI 会明确区分“记录已删除”与“向量清理完成”, 清理失败返回非零退出码。

`show` 会渲染作业统计与事件日志。事件表里带**文档列** —— 因为 `run` 失败时
提示语是"看是哪几篇", 不显示文档就兑现不了这句话。

---

## 9. DI 容器

`container.py` 将实际配置和运行时纳入 Host 的生命周期:

```text
runtime_settings   Dependency   实际 AppSettings, 保留 SecretStr 真实值
db_engine          Resource     SQLite 异步引擎
session_factory    Resource     共享会话工厂, 每个操作创建自己的 session
runtime            Resource     工作队列、检索、对话、Memory 与惰性向量连接
```

Runtime 持有一个惰性打开的 Zvec sink, 不为每次请求重复打开 collection。
原生向量读写经过互斥与取消保护; Host 停止后统一释放连接。每次扫描创建
独立 embedder 并在 finally 中关闭。原生 Memory 使用独立 embedding 配置、
Zvec collection 和异步 MAF 提取/摘要客户端，不与知识向量库共享连接。
其原生索引 I/O 通过互斥与取消保护的 `asyncio.to_thread` 执行，
由现有 Memory worker 处理持久 Operation、Outbox 重试与索引重建。

---

## 10. 本轮没做的事

- **OCR。** 扫描件 PDF 目前明确报告无可用正文，不会生成成功索引。
- **真实 embedding 端点从未接触过。** 没有 API Key, 目前只有 `MockTransport`
  层面的验证。协议层是对的, 但真实端点的错误码、限流行为都没试过。
- **并发。** 目前一篇一篇处理。IO 等待是主要成本, 加并发有收益, 但会让
  "单篇失败不中断整轮"和事件日志的顺序都变复杂, 留到有实测数据之后再说。
- **重跑 pending。** `--no-embed` 留下的 `vector_state=pending` 目前只能靠
  普通扫描重读、重切并补齐, 还没有“不读源文件、只补向量”的专用入口。
- **并行扫描写入。** Host 队列当前串行消费所有扫描、资源变更和清理,
  并用 OS 锁阻止第二个 Runtime 写入相同数据库/向量目录。尚未实现多工作者
  扫描租约; 直接调用底层 Service 的外部程序也必须遵守单写者约束。
- **向量维度迁移。** 模型版本变化会触发重处理, 但不同维度仍要求先准备匹配的
  collection; `--force` 不会销毁或改造已有 collection。
