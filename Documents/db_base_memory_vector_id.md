# `memory_vector_id` 设计说明

原生 Memory 以 SQLite 为权威存储，Zvec 仅保存可以重建的语义索引。
确定性向量 ID 用于把索引命中、revision 和持久清理意图对应到同一条记忆。

## 1. 身份规则

`backend/src/shovel/db/base.py` 中的函数保持独立于 `chunk_vector_id`：

```python
def memory_vector_id(memory_kind: str, memory_id: str, rev: int = 1) -> str:
    return str(uuid.uuid5(SHOVEL_NAMESPACE, f"mem|{memory_kind}|{memory_id}|{rev}"))
```

| 组成 | 含义 |
|---|---|
| `mem` | 区分记忆与文档 chunk 的 key 空间 |
| `memory_kind` | 原生 Memory 类型：semantic、episodic、procedural |
| `memory_id` | SQLite 中记忆记录的持久 ID |
| `rev` | 记录的 revision，默认从 1 开始 |

相同输入总是生成相同 UUID；改变类型、记录 ID 或 revision 会改变向量身份。
`ZvecMemoryIndex` 的写入和删除均复用该函数，不额外维护另一套随机向量 ID。

文档 chunk 的身份由资源、文档内容、chunker 版本、粒度和位置共同决定；
记忆没有文档 URI 或 chunk 序号，因此不应通过空参数硬套 `chunk_vector_id`。

## 2. Revision 不等于生命周期

revision 参与向量身份，但不意味着每次访问计数或来源合并都要生成新版本。
更新长期事实时，当前管理流程创建新记录并 supersede 旧记录，而不是简单覆盖历史。

| 操作 | SQLite 权威记录 | Zvec 派生索引 |
|---|---|---|
| 新建 | 新的 active 记录 | Outbox 请求写入该记录/revision 的向量 |
| Supersede | 保留旧记录及其替代关系，新记录 active | 清理旧向量，写入新向量 |
| Expire | 旧记录变为 expired | 排队清理向量 |
| 软删除 | 保留 deleted 状态和防止自动重学的指纹 | 排队清理向量 |
| 硬删除 | 删除记录，但保留清理意图 | 排队清理向量 |

历史保存在 SQLite，不依赖保留 superseded 向量。
管理 API 可通过 `include_history=true` 查看生命周期历史；
正常 Agent 召回不包含 superseded、expired 或 deleted 记录。

## 3. 最终一致性与重建

SQLite 事务同时保存事实变更与 `MemoryIndexOutbox`。
Zvec 不参与该事务：写入或删除成功并 flush 后，才能确认相应意图完成。
失败保留重试记录，不回滚已保存的事实。

检索命中仍须加载 SQLite 记录并复核 Scope、类型、状态、revision、
embedding model version 和有效期。残留向量不能恢复已删除或失效的记忆。
向量 UUID 本身不提供权限隔离；隔离依赖索引分区、过滤和 SQLite 权威校验。

模型版本/维度变化、索引丢失或显式重建时，从 SQLite 重新生成 embedding。
新 generation 内仍使用上述确定性 ID；generation、模型版本及索引状态另行追踪。
切换前等待待处理变更完成，不会因为删除索引而丢失权威记忆。
旧 generation 目录不会自动清理，重建可能产生 embedding 服务费用。

## 4. 实现与验证

- `backend/src/shovel/memory/sqlite_repository.py`：生命周期、来源、Outbox 与事务。
- `backend/src/shovel/memory/zvec_index.py`：向量身份、过滤、flush、generation 和重建。
- `backend/tests/test_native_memory.py`：真实 SQLite/Zvec 的隔离、删除、失败重试与重建竞争测试。

配置、预算及运行边界见 [README 的 Native Memory 章节](../README.md#native-memory)。
