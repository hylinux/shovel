# 函数`memory_vector_id`设计说明

一、和 chunk_vector_id 是同一个机制，不同的"身份定义"
两个函数都在回答同一个问题——"这个向量是谁？"——只是记忆和 chunk 的"身份"由不同的东西决定。

python

chunk:  f"{doc_uri}|{content_hash}|{chunker_version}|{granularity}|{index}"
memory: f"mem|{memory_kind}|{memory_id}|{rev}"
chunk 的 key 有 5 段，记忆只有 3 段（加前缀 4 段）。少掉的那两段不是省略，是不适用。

二、三个组成，逐个看
memory_id —— 身份主体
python

mvid('episodic', 'epi_01JB', 1)
epi_01JB 就是 memory_episode.id。这一段单独就能唯一确定"是哪条记忆"——不像 chunk 需要 doc_uri + index 两段才能定位。

这是记忆和 chunk 最本质的差别：一条记忆是一个独立个体，一个 chunk 是某个文档的第 N 片。

rev —— 失效触发器
实测三次修订：


rev=1  →  aaf0a0aa-...
rev=2  →  5211e25d-...
rev=3  →  dd4a628d-...
这一段的位置，对应的正是 chunk key 里的 content_hash。

chunk	memory
什么触发新 id	content_hash 变了	rev 递增
谁改的	源文件被编辑（外部）	agent 修订了这条记忆（内部）
怎么知道变了	扫描时算指纹	写入时显式 +1
为什么记忆不用 hash 内容：记忆的文本可能一字未改，但它的含义变了——比如置信度调整、证据合并、实体链接修正。内容哈希对这些是瞎的，而 rev 是显式的意图表达。

反过来，chunk 不能用 rev：源文件被用户在编辑器里改了，没人会来给 Shovel 递一个版本号，只能靠哈希发现。

memory_kind —— 防御性隔离

episodic   + 'x_1'  →  101be260-...
procedural + 'x_1'  →  79c48a2e-...
严格说，memory_id 已经带了类型前缀（epi_ / proc_），所以理论上不会撞。

但 memory_kind 仍然值得放进去，理由是它把"不撞"从一个约定变成了一个保证：

id 前缀是 new_id("epi") 的一个惯例——将来有人写个迁移脚本、或者从别处导入记忆，可能用别的前缀
而 key 里的 memory_kind 来自枚举 MemoryKind，是类型系统保证的
依赖约定 vs 依赖类型，后者更可靠。 这和 granularity 必须进 chunk key 是同一类考虑。

三、mem| 前缀在防什么
这是最容易被当成"多此一举"的一段。它防的是两个 key 空间意外产生相同字符串。

假设没有前缀，记忆的 key 是 "episodic|epi_1|1"。那么理论上只要有一个 chunk 的：


doc_uri         = "episodic"
content_hash    = "epi_1"
chunker_version = "1"
granularity     = ""          # 空
index           = ""          # 空
就会拼出同样的字符串。

现实中会发生吗？几乎不可能——doc_uri 总是 file:/// 或 msgid: 开头，index 总是整数。

那为什么还要加？ 因为这类防御的成本是 4 个字符，而代价是两个语义完全不同的东西共用一个 id——一条记忆和一个文档分块，删一个会连带删掉另一个。

这和上一条讲的 | 分隔符问题是同一个家族：当前所有字段都是受控的，但受控是今天的事实，不是结构上的保证。前缀让它变成后者。

四、为什么不能硬套 chunk_vector_id
docstring 里那句是核心：

Forcing both through one function would mean passing dummy values for two parameters, and dummy values are where collisions come from.

假设强行复用：

python

chunk_vector_id(
    doc_uri="",              # ← 记忆没有文档
    content_hash=memory_id,
    chunker_version=str(rev),
    granularity=memory_kind,
    index=0,                 # ← 记忆只有一个向量
)
三个问题：

两个参数填假值（"" 和 0），而假值一旦有两处，撞车概率就不再为零
语义完全错位——content_hash 里装的是 id，chunker_version 里装的是 rev，读代码的人会被彻底误导
改一个会影响另一个——将来给 chunk key 加一段（比如加 embedder_version），记忆的 id 会无声地全变
第 3 点最致命。 两个函数分开，chunk 侧的演进和记忆侧完全解耦。

五、rev 的默认值 = 1 是个体贴设计
python

def memory_vector_id(memory_kind: str, memory_id: str, rev: int = 1) -> str:
对应 ORM 里：

python

rev: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
绝大多数记忆一辈子只有一个版本——用户说了句偏好，存下来，再也不改。给 rev 一个默认值，让最常见的调用只写两个参数：

python

memory_vector_id(MemoryKind.EPISODIC, ep.id)
注意 chunk 侧没有任何默认值——因为它的五个组成没有一个是"通常不用管"的。

六、一个和记忆语义紧密相关的推论
回想记忆子系统那条硬规则：失效不删除，被推翻的记忆保留 superseded 状态。

那么问题来了：被 supersede 的旧记忆，它的向量还在吗？

在。而且这正是 rev 的设计意义：

操作	id 变化	向量
记忆被修订（同一条，改内容）	rev +1 → 新 id	旧 rev 的向量可以清理
记忆被推翻（新记忆取代旧的）	是两条不同的 memory_id	两条向量都保留 ★
记忆被撤回（用户说"忘掉"）	—	删除
★ 这就是为什么 purge_memory() 的 docstring 写着"只删 retracted"：

python

def purge_memory(root, memory_ids, *, settings=None):
    """Remove RETRACTED memories only.

    Superseded memories keep their vector: "what did we used to believe?" must
    stay answerable.
    """
"我们之前为什么选 SQLite" 必须仍然可答——那条旧信念的向量还在库里，只是 validity_state='superseded'，检索时按需过滤。

七、一句话总结
chunk_vector_id 的身份是"哪个文档的哪一片、什么内容、怎么切的"； memory_vector_id 的身份是"哪条记忆的第几版"。

同一套确定性哈希机制，套在两种完全不同的身份定义上——所以是两个函数，不是一个带默认值的函数。


test_vector_store.py
 里 test_memory_revision_yields_a_new_id 和 test_memory_ids_live_in_a_separate_key_space 分别守着 rev 和前缀这两条。

