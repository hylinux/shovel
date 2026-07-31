# Shovel 学习路线规划

## 文档信息

| 项目 | 内容 |
|------|------|
| 文档名称 | Shovel Learning Roadmap |
| 项目名称 | Shovel AI Agent Platform |
| 作者 | HongWei Guo |
| 版本 | v1.0 |
| 更新时间 | 2026/7/31 |
| 路径 | docs/shovel-learning-roadmap.md |

---

# 1. 文档目标

本学习路线用于指导 Shovel 项目的技术学习和落地实施。

目标不是单独学习某个框架，而是围绕 Shovel 产品架构：

```text
CLI
↓
Host
↓
Database
↓
Web API
↓
Agent Runtime
↓
Knowledge
↓
Memory
↓
Web UI
```

逐步完成整个系统。

---

# 2. 学习原则

## 2.1 项目驱动学习

不要：

```text
学 Typer
学 Rich
学 FastAPI
学 SQLAlchemy
```

而应该：

```text
为了实现 shovel init 学 Typer

为了实现 Rich Console 学 Rich

为了实现 Agent API 学 FastAPI

为了实现数据存储学 SQLAlchemy
```

---

## 2.2 每个阶段必须有产出

每学习一个模块必须有可运行代码。

例如：

```text
学习 Typer
    ↓
产出 shovel init

学习 Rich
    ↓
产出 Shovel Console

学习 SQLAlchemy
    ↓
产出 AgentRepository
```

---

## 2.3 优先学骨架

优先顺序：

```text
Host
Configuration
DI
CLI
Database
```

而不是：

```text
UI
Workflow
多 Agent
高级功能
```

---

## 2.4 保持可交付

每个阶段结束时：

应该能够：

```bash
git commit
```

并形成稳定增量。

---

# 3. 总体学习路线

建议按照以下顺序学习：

```text
阶段 1
Typer

阶段 2
Rich

阶段 3
Configuration

阶段 4
Dependency Injection

阶段 5
Generic Host

阶段 6
SQLAlchemy

阶段 7
Alembic

阶段 8
FastAPI

阶段 9
Microsoft Agent Framework

阶段 10
Qdrant

阶段 11
Mem0

阶段 12
Redis

阶段 13
Next.js

阶段 14
CSS + Tailwind

阶段 15
OpenTelemetry

阶段 16
Packaging
```

---

# 阶段 1：Typer

---

## 学习目标

掌握：

```python
import typer
```

构建企业级 CLI。

---

## 必学内容

### Typer Application

```python
app = typer.Typer()
```

---

### Command

```python
@app.command()
def init():
    ...
```

---

### Option

```python
--port
--host
--reload
```

---

### Argument

```bash
shovel agent run support
```

support 即 argument。

---

### Callback

```python
@app.callback()
```

---

### Subcommand

```bash
shovel agent
shovel config
shovel database
```

---

## Shovel 输出

完成：

```bash
shovel init
shovel doctor
shovel start
```

---

## 学习资料

官方：

https://typer.tiangolo.com/

重点：

```text
Tutorial
Commands
Subcommands
Options
Arguments
Context
```

---

## 验收标准

能够实现：

```bash
shovel --help
shovel init
shovel doctor
```

---

# 阶段 2：Rich

---

## 学习目标

构建产品级 CLI 输出。

---

## 必学内容

### Console

```python
Console()
```

---

### Text

```python
Text()
```

---

### Table

```python
Table()
```

---

### Panel

```python
Panel()
```

---

### Status

```python
with console.status():
```

---

### Traceback

```python
rich.traceback
```

---

## Shovel 输出

实现：

```python
console.info()
console.success()
console.warning()
console.error()
```

---

## UI 规范

| 类型 | 图标 |
|--------|--------|
| info | ℹ |
| success | ✓ |
| warning | ⚠ |
| error | ✗ |

---

## 验收标准

完成：

```bash
shovel doctor
```

输出类似：

```text
✓ SQLite Ready

✓ Config Ready

✓ Runtime Ready
```

---

# 阶段 3：Pydantic Settings

---

## 学习目标

实现强类型配置。

---

## 必学内容

### BaseSettings

```python
BaseSettings
```

---

### JSON 加载

```json
settings.json
```

---

### Environment Variable

```text
SHOVEL_DEBUG=true
```

---

### Validation

```python
Field()
```

---

## Shovel 输出

完成：

```text
shovel settings.json

读取

校验

生成默认配置
```

---

## 验收标准

实现：

```bash
shovel init
```

自动创建：

```text
~/.shovel/settings.json
```

---

# 阶段 4：Dependency Injector

---

## 学习目标

统一管理依赖。

---

## 必学内容

### Container

```python
class Container(...)
```

---

### Singleton

```python
providers.Singleton
```

---

### Factory

```python
providers.Factory
```

---

### Configuration

```python
providers.Configuration()
```

---

## Shovel 输出

完成：

```text
AgentService

KnowledgeService

MemoryService

Repository
```

统一注册。

---

## 验收标准

实现：

```python
container.agent_service()
```

---

# 阶段 5：Generic Host

---

## 学习目标

实现：

```text
Python Generic Host
```

---

## 必学内容

### HostBuilder

### Host

### Lifetime

### Service Provider

---

## Shovel 输出

支持：

```python
host.run()

host.stop()
```

---

## 验收标准

实现：

```bash
shovel start
```

启动完整运行时。

---

# 阶段 6：SQLAlchemy

---

## 学习目标

构建持久化层。

---

## 必学内容

### DeclarativeBase

### Session

### Repository

### Relationships

### Transactions

---

## Shovel 输出

实现：

```text
Agent

Conversation

Message
```

---

## 验收标准

实现：

```bash
shovel agent create
```

写入 SQLite。

---

# 阶段 7：Alembic

---

## 学习目标

数据库迁移管理。

---

## 需要掌握

```bash
alembic init

alembic revision

alembic upgrade

alembic downgrade
```

---

## 验收标准

实现：

```bash
shovel database migrate
```

---

# 阶段 8：FastAPI

---

## 学习目标

构建 Web Host。

---

## 必学内容

### App

### Router

### Dependency

### Middleware

### WebSocket

---

## Shovel 输出

实现：

```text
GET /agents

POST /agents

POST /chat
```

---

## 验收标准

浏览器访问：

```text
/docs
```

可查看 API。

---

# 阶段 9：Microsoft Agent Framework

---

## 学习目标

构建 Agent Runtime。

---

## 必学内容

### Agent

### Session

### Tool

### Workflow

### State

---

## Shovel 输出

实现：

```bash
shovel agent run
```

---

## 验收标准

能够创建 Agent 并回复问题。

---

# 阶段 10：Qdrant

---

## 学习目标

实现 Knowledge Base。

---

## 学习内容

### Collection

### Embedding

### Search

### Metadata

---

## Shovel 输出

实现：

```bash
shovel kb add

shovel kb search
```

---

## 验收标准

支持文档检索。

---

# 阶段 11：Mem0

---

## 学习目标

实现长期记忆。

---

## 学习内容

### Extract Memory

### Store Memory

### Retrieve Memory

---

## Shovel 输出

支持：

```text
长期记忆
```

---

## 验收标准

用户信息能够自动记住并回忆。

---

# 阶段 12：Redis

---

## 学习目标

实现缓存和状态管理。

---

## 使用场景

```text
Session

Cache

Runtime State
```

---

## 验收标准

Agent Session 支持 Redis。

---

# 阶段 13：Next.js

---

## 学习目标

构建 Web Console。

---

## 必学内容

### React

### App Router

### Server Component

### Client Component

---

## 页面

```text
Dashboard

Agents

Chat

Knowledge

Settings
```

---

## 验收标准

能够访问：

```text
http://localhost:3000
```

---

# 阶段 14：CSS + Tailwind

---

## 学习目标

完成产品 UI。

---

## 必学内容

### Flex

### Grid

### Typography

### Responsive

### Tailwind

---

## 验收标准

完成 Dashboard UI。

---

# 阶段 15：OpenTelemetry

---

## 学习目标

实现可观测性。

---

## 需要掌握

### Trace

### Metrics

### Logging

---

## 验收标准

能够追踪：

```text
Agent Run
Tool Call
API Request
```

---

# 阶段 16：Packaging

---

## 学习目标

把 Shovel 变成真正产品。

---

## 必学内容

### pyproject.toml

### uv

### wheel

### pip install

---

## 验收标准

支持：

```bash
pip install shovel

shovel --version
```

---

# 最终项目验收

达到以下目标：

```bash
shovel init

shovel doctor

shovel start

shovel agent create

shovel agent run

shovel kb add

shovel kb search
```

浏览器访问：

```text
http://localhost:8000

http://localhost:3000
```

Agent：

```text
Memory
Knowledge
Tools
Workflow
```

全部工作正常。

---

# 推荐学习资料

## Typer

- https://typer.tiangolo.com/

## Rich

- https://rich.readthedocs.io/

## FastAPI

- https://fastapi.tiangolo.com/

## SQLAlchemy

- https://docs.sqlalchemy.org/

## Alembic

- https://alembic.sqlalchemy.org/

## Microsoft Agent Framework

- https://learn.microsoft.com/agent-framework/

## Qdrant

- https://qdrant.tech/documentation/

## Mem0

- https://docs.mem0.ai/

## Next.js

- https://nextjs.org/docs

## Tailwind

- https://tailwindcss.com/docs

---

# 总结

Shovel 的学习路线应遵循：

```text
CLI
↓
Configuration
↓
DI
↓
Host
↓
Database
↓
Web API
↓
Agent Runtime
↓
Knowledge
↓
Memory
↓
Web UI
↓
Observability
↓
Packaging
```

先构建工程骨架，再构建 Agent 能力，最后完善产品体验。

目标不是学会框架，而是完成一个真正可运行、可维护、可扩展的 AI Agent Platform。