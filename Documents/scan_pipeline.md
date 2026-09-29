# Shovel 扫描管线

> 对应代码: `exceptions/pipeline.py`、`pipeline/parsers/*`、`pipeline/chunker.py`、
> `pipeline/embedder.py`、`pipeline/sink.py`、`pipeline/scan_service.py`、
> `cli/commands/scan.py`

资源模块解决的是"怎么连上一个数据源"。扫描管线解决的是紧接着的那个问题:
**连上了之后, 怎么把里面的东西变成可以被检索的块。**

`Connector.discover()` 在资源模块里一直没有消费者 —— 本模块就是那个消费者。

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

扫描件 PDF 能正常解析, 只是每页都是空文本。这不报错, 也不产出块 —— 将来接上
OCR 就是在这里补一层, 现在先老实地记录"这篇没内容"。

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

`chunk_vector_id(doc_uri, content_hash, chunker_version, granularity, index)`。

也就是说 **改 `CHUNKER_VERSION` = 全库重切 + 重 embedding**。这是故意的:
切块逻辑变了而 id 不变, 会让旧向量和新文本在 Zvec 里共存, 而且没有任何办法
发现这件事。

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

关掉之后 chunk 照常写进 SQLite, 停在 `vector_state=pending`; 等 embedding
配好了再跑一次就能补齐。

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

---

## 6. 增量与对账

### 判据是 hash, 不是 mtime

`indexed_hash != content_hash` 才重新处理。用 `modified_at` 会在两个方向都出错:
`touch` 过的文件被白白重扫一遍, 而某些同步工具会保留原始 mtime, 于是改过的
文件被漏掉。

(`local_fs` 的 `content_hash` 本身是 `st:{size}-{mtime_ns}`, 见资源模块文档 ——
但那是连接器内部的选择, 管线这一层只认"这个串变了没有"。)

### 删除对账依赖完整的 discover

`Job.is_discovery_complete` 为假时**不做**删除对账。

理由是删除判据只能是"这一轮没见到它"。如果枚举中途挂了, 或者被 `--limit`
截断了, "没见到"就不等于"不存在了" —— 一次失败的扫描会把整个资源集体立碑。

所以 `--limit` 只截断"本轮处理多少篇", **不截断 discover**。看起来浪费,
但这是唯一能同时支持"只处理 10 篇"和"敢做对账"的做法。

### 立碑, 不删除

对账用一条 UPDATE 完成: `last_seen_run != job_id` 的都标成墓碑。

不物理删除, 是因为文件"消失"在真实世界里经常只是临时的 —— 网盘没同步完、
U 盘没插、目录被临时改名。立碑的语义是"这一轮没看见", 下一轮见到了就自动复活,
不需要任何人工干预。

---

## 7. 编排层的几处顺序敏感代码

`ScanService.scan()` 的骨架:

```text
_preflight()   解析资源, 确认状态可扫, 建 Job
_discover()    跑完整个 discover, 落成观测集
_process()     逐篇 fetch → parse → chunk
_persist()     先删旧 chunk, 再写新 chunk
_flush()       攒够 256 个向量写一次 Zvec
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
shovel scan run <资源> [--mode] [--profile] [--target] [--limit] [--no-embed]
shovel scan list [--resource] [--limit]
shovel scan show <作业 id> [--events N]
```

`show` 会渲染作业统计与事件日志。事件表里带**文档列** —— 因为 `run` 失败时
提示语是"看是哪几篇", 不显示文档就兑现不了这句话。

---

## 9. DI 容器

`container.py` 里加了四个 provider:

```text
embedding_settings   Factory     config 里的 dict 再 validate 回 pydantic 模型
embedder             Factory     带 expected_dimension 校验
chunk_sink           Singleton   背后是打开的 zvec collection, 不重复开
scan_service         Factory     一次扫描 = 一个独立实例
```

`chunk_sink` 是 Singleton 而其余是 Factory, 差别在于有没有持有句柄。
需要注意: **embedder 与 sink 的释放不归容器管**, 调用方跑完要各自 `aclose()`。

---

## 10. 本轮没做的事

- **OCR。** 扫描件 PDF 现在解析出空文本层, 不报错也不产块。
- **真实 embedding 端点从未接触过。** 没有 API Key, 目前只有 `MockTransport`
  层面的验证。协议层是对的, 但真实端点的错误码、限流行为都没试过。
- **`chunk_vector_id` 不含 `resource_id`。** 同一个文件被两个资源索引时,
  Zvec 里会 id 碰撞。这是既有模型设计, 不是本模块引入的, 暂未处理。
- **并发。** 目前一篇一篇处理。IO 等待是主要成本, 加并发有收益, 但会让
  "单篇失败不中断整轮"和事件日志的顺序都变复杂, 留到有实测数据之后再说。
- **重跑 pending。** `--no-embed` 留下的 `vector_state=pending` 目前只能靠
  重新扫一遍补齐, 还没有"只补向量"的专用入口。
