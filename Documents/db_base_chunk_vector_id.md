# 函数`chunk_vector_id`详细设计说明

一、函数在做什么：两行代码，一句话

```python
key = f"{doc_uri}|{content_hash}|{chunker_version}|{granularity}|{index}"
return str(uuid.uuid5(SHOVEL_NAMESPACE, key))
```

把"这块内容是谁"这件事描述成一个字符串，然后哈希成 UUID。

它不是"生成一个 id"，而是**"计算"**一个 id——没有随机性，没有自增，没有数据库参与。同样的输入在任何机器、任何时刻，得到完全相同的结果。

二、最关键的一句：一个 key，两个存储

```bash
                 chunk_vector_id(...)
                         │
          ┌──────────────┴──────────────┐
          ↓                             ↓
   SQLite: chunk.id              Zvec: Doc.id
   ├─ text（权威全文）            ├─ dense 向量
   ├─ chunk_index                 ├─ 过滤用的标量字段
   └─ parent_chunk_id             └─ FTS 文本

```

这两个 id 是同一个值。 由此带来三件事：

| 操作 | 怎么做 |
| --- | --- |
| join | 	Zvec 返回一批 id → 直接 WHERE chunk.id IN (...) 取全文 |
| 对账	| 两边各拉一份 id 集合，做差集就知道谁多了谁少了 |
| 同删	| 一个 id 列表，SQLite 删一次、Zvec 删一次 |


如果两边 id 不同，你就需要第三张映射表 chunk_id ↔ vector_id。而那张表会立刻成为新的故障点：它自己可能不一致、可能落后、可能在崩溃时半写。

这正是"SQLite 是真相之源，Zvec 可丢弃重建"能成立的技术前提——删掉 Zvec 重建时，id 能被重新算出来，不需要从映射表里恢复。

三、确定性换来幂等性
场景：一次索引跑到一半崩了

```bash
处理 500 个 chunk → 崩在第 300 个
重跑 → 前 300 个重新生成 id
``

| id 策略 | 	结果 |
| --- | --- |
| uuid4() 随机	| 前 300 个拿到全新 id → Zvec 认为是新记录 → 每个向量存了两份 |
| chunk_vector_id() | 	前 300 个拿到相同 id → upsert 覆盖 → 干净 |

随机 id 的真正麻烦不是"多了一份"，而是你没有任何办法分辨哪些是副本。两条记录内容一模一样、id 不同，你凭什么删其中一条？

检索结果里同一段话出现两次、三次，而且随着每次重试越积越多。


四、五个 key 组成，逐个看它防什么
我实测跑了一遍（见上面的输出），下面用真实 id 说明。

doc_uri —— 两份内容相同的文件必须分开

```bash
file:///a/LICENSE   内容 = MIT 协议全文
file:///b/LICENSE   内容 = MIT 协议全文    ← content_hash 完全一样
```

不放 doc_uri，这两份文件的第 0 块会撞 id 互相覆盖。而本地磁盘上内容相同的文件非常多：LICENSE、README 模板、从同一个地方下载两次的 PDF。

content_hash —— 文件改了，旧向量必须失效

```bash
改前 chunk #0 → 89ab4dd7-...
改后 chunk #0 → b39e4d3e-...   ← 完全不同

```

这是整个"影子写入"能成立的前提：新版本用新 id 先写进去，确认成功后再删旧 id 的那些。中途崩溃时用户检索到的是旧版本，而不是空洞。

不放它的话，文件改了但 id 不变 → 写入是覆盖 → 中途崩溃就是数据丢失。

chunker_version —— 切分策略换了，旧块整批退休

```bash
chunker_version = "md-v2"   # 从 md-v1 升级，比如改了标题层级的处理
```

所有 id 全变，等于宣告"旧切法产生的向量全部作废"。配合 purge_stale_model 那类清理，能干净地完成一次 chunker 升级。

不放它：升级后新旧两种切法的块混在一个库里，边界重叠、内容重复，而你无法区分。

granularity —— 最不直观的一条 

看实测输出：

```bash
chunk  #0  →  89ab4dd7-...
parent #0  →  ab21f962-...
```

这两条记录，doc_uri、content_hash、chunker_version、index 四项完全一样——只有 granularity 不同。

漏掉它，父块会静默覆盖它的第一个子块。 而这两者恰恰是最重要的一对：子块负责被检索（精度），父块负责被阅读（expand_chunk 的目标）。丢了子块，检索直接失准。

这就是为什么 Granularity 必须进 key —— 它是"同一段文本的不同角色"，而 id 必须区分角色。

index —— 否则一个文档塌缩成一条

```bash
chunk #0  →  89ab4dd7-...
chunk #1  →  93580902-...
chunk #2  →  7cea9abe-...
```

不放 index，一个文档的 120 个块全部拿到同一个 id，互相覆盖到只剩最后一个。

五、为什么是 uuid5 而不是 sha256
uuid5(namespace, name) 本质是 SHA1(namespace_bytes + name) 取前 16 字节，再按 UUID 规则打上版本位。

三个理由：

1. 格式 — 向量库普遍要求 id 是 UUID 或整数。裸的 sha256 十六进制串（64 字符）不一定被接受，而 UUID 是通用货币。

2. 命名空间隔离 — 这是 SHOVEL_NAMESPACE 存在的全部意义：

```bash
uuid5(SHOVEL_NS,  "file:///a.md|h1|...")  →  89ab4dd7-...
uuid5(OTHER_NS,   "file:///a.md|h1|...")  →  完全不同的值
```

别的工具哈希同样的字符串，不会撞到我们的 id 空间。

这也是之前把 uuid.NAMESPACE_DNS 换掉的原因——那是 RFC 4122 的公共命名空间，等于放弃了隔离。

3. 长度固定 — 36 字符，索引友好，两个存储的主键长度一致。

六、一个重要提醒：SHOVEL_NAMESPACE 不能改

```bash
SHOVEL_NAMESPACE = uuid.UUID("0fcdde6d-5f73-498e-9ebc-1b09e5254c71")
```

它是持久化格式的一部分，和 schema version 同级。

改了它 → 同样的内容算出不同的 id → 重索引变成"新增一份"而不是"覆盖" → 整个库需要重建。

这就是为什么它旁边的注释写着 "FROZEN"，以及为什么我在换 Zvec 那一轮顺手修它——换库本来就要全量重建向量，那是修改它的唯一零成本窗口。


七、key 用 | 分隔的一个隐含前提
python

key = f"{doc_uri}|{content_hash}|{chunker_version}|{granularity}|{index}"
这依赖一个假设：doc_uri 里不含 |。

现实中基本成立——文件 URI 会把 | 转义成 %7C，msgid: 也不含它。但严格说这是可构造碰撞的：


doc_uri="a|b", content_hash="c"   →  "a|b|c|..."
doc_uri="a",   content_hash="b|c" →  "a|b|c|..."   ← 同一个 key
真正严谨的做法是长度前缀（len(field):field）或用不可能出现的分隔符（
\x00
）。

为什么当前写法可以接受：doc_uri 由 connector 生成（都是标准 URI），content_hash 是十六进制，chunker_version 和 granularity 是受控的枚举值，index 是整数——五个字段里没有一个是用户自由输入的。

但这值得记一笔：如果将来有 connector 产出非 URI 形式的 doc_uri（比如直接用文件路径），这个假设就要重新检查。

八、一句话总结
它把"这块内容的身份"编码成一个可重算的 UUID，让 SQLite 和 Zvec 共用一个主键、让流水线天然幂等、让内容变更自动失效旧向量——全部不需要数据库分配 id，也不需要映射表。