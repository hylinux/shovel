# Shovel 架构与设计说明文档

## 文档信息

| 项目 | 内容 |
|---|---|
| 文档名称 | Shovel 架构与设计说明文档 |
| 项目名称 | Shovel AI Agent Platform |
| 文档类型 | 架构设计说明 / 技术设计说明 |
| 作者 | HongWei Guo |
| 版本 | Draft v1.0 |
| 目标读者 | 项目开发者、架构设计者、后续维护者 |
| 更新时间 | 2026/7/31 |
| 路径 | `docs/shovel-architecture-design.md` |

---

## 1. 执行摘要

Shovel 的目标不是一个单独的 CLI 工具，也不是一个简单的 Chat UI，而是一个面向个人和本地开发场景的 **AI Agent Platform**。

它通过统一的 CLI、Web API 和 Web UI，提供 Agent 的创建、运行、管理、知识检索、记忆、工具调用与工作流编排能力。

从架构上看，Shovel 借鉴 `.NET Generic Host` 的思想，以 `HostBuilder` 作为应用启动入口，把配置、日志、依赖注入、数据库、Agent Runtime、Web Server 和资源生命周期统一管理。

Shovel 的最终定位可以总结为：

> Shovel 是一个采用 Python Generic Host 架构实现的全栈 AI Agent Platform，通过 Typer + Rich 提供 CLI 体验，通过 FastAPI + Next.js 提供 Web 体验，以 Microsoft Agent Framework 为核心 Agent Runtime，并集成 SQLAlchemy、Qdrant、Mem0、Redis 等组件，实现 Agent、Memory、Knowledge、Tools 和 Workflow 的统一管理。

---

## 2. 产品定位

### 2.1 Shovel 是什么

Shovel 可以被理解为一个面向 Agent 开发、运行和管理的本地优先平台。

它不是单一功能工具，而是由多个核心系统组成：

```text
Shovel
=
  CLI Runtime
+ Web API Host
+ Agent Runtime
+ Knowledge System
+ Memory System
+ Tool System
+ Workflow System
+ Local Storage
+ Web UI
```

### 2.2 Shovel 不是什么

Shovel 不应该被设计成：

- 单纯的命令行工具
- 单纯的聊天页面
- 单纯的 RAG Demo
- 单纯的数据库管理工具
- 单纯的 Agent Framework wrapper
- 单纯的文档索引工具
- 单纯的 Prompt 管理器

这些功能都可以是 Shovel 的一部分，但不能代表 Shovel 的完整定位。

### 2.3 Shovel 应该是什么

Shovel 应该是：

- 一个本地优先的 AI Agent 平台
- 一个统一管理 Agent、Knowledge、Memory、Tools、Workflow 的产品
- 一个支持 CLI 和 Web UI 双入口的开发者工具
- 一个具备长期演进能力的 Python 工程项目
- 一个可以从本地运行平滑扩展到云端部署的平台
- 一个具备良好工程结构、类型安全、可测试性和可观测性的产品化项目

---

## 3. 核心设计目标

### 3.1 工程化

Shovel 不应该只是几个脚本拼接在一起，而应该有清晰的项目结构、模块边界、依赖关系和生命周期管理。

工程化目标包括：

- 清晰的目录结构
- 稳定的模块边界
- 统一的启动入口
- 统一的配置管理
- 统一的日志系统
- 统一的依赖注入
- 可测试的 Application Service
- 可替换的 Infrastructure 实现

### 3.2 可扩展

后续可能增加新的 Agent Runtime、新的数据库、新的向量数据库、新的 Tool 类型、新的前端页面，因此架构必须预留扩展空间。

需要重点支持：

- SQLite 到 PostgreSQL 的平滑迁移
- Local Qdrant 到 Cloud Qdrant 的迁移
- Local model 到 Azure OpenAI 或 OpenAI 的迁移
- Simple memory 到 Mem0 的替换
- 内置 Tool 到 MCP Tool 的扩展
- CLI-only 到 Web Console 的扩展
- Single Agent 到 Multi-Agent Workflow 的扩展

### 3.3 可测试

CLI、Application Service、Repository、Agent Runtime、FastAPI Route 都应该可以单独测试。

建议测试层次：

```text
unit tests
integration tests
cli command tests
api tests
repository tests
agent runtime tests
```

### 3.4 本地优先

第一阶段先保证本地开发体验：

```text
SQLite
local config
local CLI
local FastAPI
local Next.js
local vector store
local memory
```

后续再考虑云端部署。

### 3.5 产品化体验

Shovel 的 CLI 输出、错误提示、配置初始化、启动流程、目录生成和 Web UI 都应该具有产品级体验，而不是简单的 demo 输出。

产品化体验包括：

- 清晰的命令帮助
- 友好的错误提示
- 彩色状态输出
- 启动过程可观察
- 初始化过程可解释
- 配置文件可读
- Web UI 简洁可用

---

## 4. 总体架构

Shovel 建议采用分层架构：

```text
┌──────────────────────────────────────────────┐
│ Presentation Layer                            │
│                                              │
│  CLI: Typer + Rich                            │
│  Web UI: Next.js + CSS/Tailwind               │
│  Web API: FastAPI                             │
└──────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────┐
│ Application Layer                             │
│                                              │
│  AgentService                                 │
│  ChatService                                  │
│  KnowledgeService                             │
│  MemoryService                                │
│  ToolService                                  │
│  WorkflowService                              │
│  SettingsService                              │
└──────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────┐
│ Domain Layer                                  │
│                                              │
│  Agent                                        │
│  Conversation                                 │
│  Message                                      │
│  Tool                                         │
│  KnowledgeBase                                │
│  Document                                     │
│  Memory                                       │
│  Workflow                                     │
│  Settings                                     │
└──────────────────────────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────┐
│ Infrastructure Layer                          │
│                                              │
│  SQLAlchemy / Alembic                         │
│  SQLite / PostgreSQL                          │
│  Qdrant                                       │
│  Redis                                        │
│  Mem0                                         │
│  Microsoft Agent Framework                    │
│  OpenTelemetry                                │
│  Logging                                      │
│  Dependency Injection                         │
└──────────────────────────────────────────────┘
```

---

## 5. 技术栈设计

| 模块 | 技术 | 在 Shovel 中的职责 |
|---|---|---|
| CLI | Typer | 定义 `shovel init`、`start`、`doctor`、`agent`、`config`、`database` 等命令 |
| Terminal UI | Rich | 统一 info、success、warning、error、panel、table、status 输出体验 |
| Web API | FastAPI | 提供 REST API、WebSocket Streaming Chat、OpenAPI schema |
| Frontend | Next.js + CSS/Tailwind | 提供 Agent、Conversation、Knowledge、Settings 的 Web Console |
| Agent Runtime | Microsoft Agent Framework | 创建、运行、编排 Agent，处理工具、会话、上下文和 workflow |
| Configuration | Pydantic Settings | 以强类型方式管理 settings.json、env、默认配置和运行时配置 |
| Dependency Injection | dependency-injector | 管理应用服务、基础设施服务和运行时组件 |
| ORM | SQLAlchemy 2.0 | 管理关系数据库访问，支持 SQLite 到 PostgreSQL 的演进 |
| Migration | Alembic | 管理数据库 schema 变更与版本迁移 |
| Vector Store | Qdrant | 存储 document chunks 和 embedding，支持语义检索 |
| Memory | Mem0 | 管理长期记忆、用户偏好和 Agent 记忆 |
| Cache / State | Redis | 缓存、会话状态、临时运行状态、任务状态 |
| Observability | OpenTelemetry + Logging | 跟踪 Agent、Tool、API、DB 的调用链和诊断信息 |
| Packaging | pyproject.toml + uv | 管理依赖、构建、安装和 CLI entry point |
| Testing | pytest | 单元测试、集成测试和回归测试 |
| Linting | Ruff | 代码格式化和静态检查 |
| Type Checking | mypy | 类型检查和接口稳定性保障 |

---

## 6. Generic Host 设计思想

### 6.1 为什么需要 Generic Host

如果没有统一 Host，Shovel 很容易变成：

```text
CLI 自己初始化配置
FastAPI 自己初始化配置
Agent Runtime 自己初始化配置
Database 自己初始化连接
每个模块各自管理生命周期
```

这样会导致：

- 初始化逻辑重复
- 配置来源混乱
- 依赖对象难以替换
- 测试困难
- 启动和关闭流程不可控
- CLI 和 Web API 行为不一致
- 后续扩展后台任务或 Web Host 时复杂度升高

因此 Shovel 应该使用类似 `.NET Generic Host` 的设计思想。

### 6.2 推荐启动模型

```python
host = (
    HostBuilder()
        .configure_configuration()
        .configure_logging()
        .configure_services()
        .configure_web_host()
        .build()
)

host.run()
```

### 6.3 Host 负责什么

Host 应该统一管理：

- Configuration
- Logging
- Dependency Injection Container
- Database Engine
- Session Factory
- Agent Runtime
- Web Server
- Background Services
- Graceful Shutdown
- Resource Disposal

### 6.4 HostBuilder 的职责

HostBuilder 负责构造 Host。

```text
HostBuilder
├── configure_configuration()
├── configure_logging()
├── configure_services()
├── configure_web_host()
└── build()
```

### 6.5 Host 的职责

Host 负责运行应用。

```text
Host
├── start()
├── run()
├── stop()
└── dispose()
```

### 6.6 Host 与 CLI 的关系

CLI command 不应该直接初始化所有依赖，而应该通过 HostBuilder 构建运行环境。

例如：

```text
shovel start
    ↓
create HostBuilder
    ↓
load settings
    ↓
configure logging
    ↓
configure services
    ↓
build host
    ↓
host.run()
```

---

## 7. 配置系统设计

### 7.1 推荐方案

Shovel 的配置系统建议继续以 `pydantic-settings` 为核心。

原因：

- 强类型配置
- 默认值清晰
- 环境变量支持好
- 与 Pydantic 模型一致
- 适合产品级应用配置
- 方便生成默认配置文件
- 方便校验配置错误
- IDE 支持较好

### 7.2 配置优先级

建议配置加载优先级如下：

```text
1. CLI options
2. Environment variables
3. .env
4. settings.json
5. Default values
```

### 7.3 推荐配置模型

```text
AppSettings
├── app_name
├── environment
├── debug

DatabaseSettings
├── provider
├── connection_string

WebSettings
├── host
├── port
├── reload

AgentSettings
├── default_model
├── temperature
├── max_tokens

QdrantSettings
├── url
├── collection_name

RedisSettings
├── host
├── port
├── db

MemorySettings
├── provider
├── enabled

TelemetrySettings
├── enabled
├── endpoint
├── service_name
```

### 7.4 Pydantic Settings 与 Dependency Injector Configuration 的关系

建议：

```text
Pydantic Settings
    作为全局强类型配置模型

Dependency Injector Configuration
    作为 DI 容器内部配置装配方式
```

不建议直接用 Dependency Injector 的 Configuration 替代 Pydantic Settings。

原因是 Shovel 更需要：

- 明确的配置模型
- 类型校验
- 默认值
- 配置文件生成
- IDE 类型提示
- 可读性较好的 settings.json
- 未来向用户暴露配置说明

### 7.5 默认配置生成

Shovel 应该在 `shovel init` 时生成默认配置。

推荐路径：

```text
~/.shovel/settings.json
~/.shovel/logs/
~/.shovel/data/
~/.shovel/cache/
```

推荐命令：

```bash
shovel init
```

推荐行为：

```text
1. 检查 ~/.shovel 是否存在
2. 检查 settings.json 是否存在
3. 如果不存在，生成默认配置
4. 如果目录不存在，创建目录
5. 输出清晰的初始化结果
```

---

## 8. 依赖注入设计

### 8.1 为什么需要依赖注入

Shovel 中会有大量服务：

```text
AgentService
KnowledgeService
MemoryService
ToolService
ConversationRepository
AgentRepository
QdrantClient
RedisClient
DatabaseSession
AgentRuntime
```

如果全部手动创建，代码会很快变乱。

### 8.2 推荐 Container 结构

```text
Container
├── settings
├── logger
├── database_engine
├── session_factory
├── agent_repository
├── conversation_repository
├── message_repository
├── settings_repository
├── agent_service
├── chat_service
├── knowledge_service
├── memory_service
├── tool_service
├── workflow_service
└── agent_runtime
```

### 8.3 设计原则

- CLI 不直接 new service
- FastAPI route 不直接 new service
- Application service 不直接 new database connection
- Infrastructure 层负责具体实现
- Domain 层保持纯净
- 测试时可以替换 repository、runtime、client
- 运行时组件由 Host 负责管理生命周期

### 8.4 CLI 与 DI 的关系

推荐流程：

```text
Typer command
    ↓
Build host
    ↓
Resolve service
    ↓
Call application service
    ↓
Render result with Rich
```

### 8.5 FastAPI 与 DI 的关系

推荐流程：

```text
FastAPI route
    ↓
Depends / container provider
    ↓
Application service
    ↓
Repository / runtime
    ↓
Response schema
```

---

## 9. CLI 设计

### 9.1 CLI 的定位

CLI 是 Shovel 的第一入口。

它应该像 `git`、`docker`、`dotnet` 一样，提供稳定清晰的命令结构。

### 9.2 推荐命令

```text
shovel --version

shovel init
shovel doctor
shovel start

shovel config show
shovel config set

shovel agent list
shovel agent create
shovel agent run
shovel agent delete

shovel database migrate
shovel database downgrade
shovel database reset

shovel kb add
shovel kb search
shovel kb list
```

### 9.3 CLI 目录结构

```text
shovel/
└── cli/
    ├── app.py
    ├── main.py
    ├── console.py
    ├── output.py
    ├── styles.py
    └── commands/
        ├── init.py
        ├── start.py
        ├── doctor.py
        ├── config.py
        ├── agent.py
        ├── database.py
        └── knowledge.py
```

### 9.4 Rich 输出标准

建议统一封装 `ShovelConsole`：

```python
console.info("Checking default directories.")
console.success("The default profile directory exists.")
console.warning("Config file not found, generated default config.")
console.error("Failed to start Shovel host.")
```

### 9.5 输出语义

| 类型 | 颜色 | 图标 | 使用场景 |
|---|---|---|---|
| info | blue | ℹ | 普通运行信息 |
| success | green | ✓ | 成功完成 |
| warning | yellow | ⚠ | 非阻塞警告 |
| error | red | ✗ | 阻塞错误 |
| debug | dim | • | 调试信息 |

### 9.6 CLI 错误处理

建议所有 command 包一层统一异常处理：

```text
command_handler
├── 捕获 ShovelException
├── 捕获 ValidationError
├── 捕获 KeyboardInterrupt
├── 捕获未预期异常
└── 转换为稳定 exit code
```

推荐 exit code：

| Exit Code | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 一般错误 |
| 2 | 参数错误 |
| 3 | 配置错误 |
| 4 | 依赖检查失败 |
| 130 | 用户中断 |

---

## 10. 数据库与持久化设计

### 10.1 第一阶段数据库选择

第一阶段建议使用 SQLite。

原因：

- 本地优先
- 无需额外部署
- 适合 CLI 和本地开发
- 与 SQLAlchemy 配合简单
- 后续可以迁移到 PostgreSQL

### 10.2 ORM 选择

建议使用：

```text
SQLAlchemy 2.0
```

原因：

- Python ORM 事实标准
- 支持 SQLite、PostgreSQL、MySQL、SQL Server
- 适合长期项目
- 支持同步和异步模式
- 与 Alembic 配套成熟

### 10.3 Migration 选择

建议使用：

```text
Alembic
```

用途：

- 创建数据库 schema 版本
- 管理表结构变更
- 支持 upgrade 和 downgrade
- 支持自动生成 migration

### 10.4 第一阶段数据模型

```text
agents
conversations
messages
settings
```

#### agents

用于存储 Agent 元数据：

```text
id
name
description
instructions
model
enabled
created_at
updated_at
```

#### conversations

用于存储会话：

```text
id
agent_id
title
created_at
updated_at
```

#### messages

用于存储消息：

```text
id
conversation_id
role
content
tool_call_id
created_at
```

#### settings

用于存储本地设置：

```text
key
value
updated_at
```

### 10.5 后续扩展数据模型

```text
documents
chunks
tools
runs
workflow_runs
memories
```

### 10.6 数据库命令设计

推荐 CLI：

```bash
shovel database migrate
shovel database downgrade
shovel database reset
shovel database status
```

推荐行为：

```text
database migrate
    执行 alembic upgrade head

database downgrade
    执行 alembic downgrade -1

database reset
    删除本地数据库并重新初始化

database status
    查看当前 migration version
```

---

## 11. Repository 设计

### 11.1 为什么需要 Repository

不建议在 Application Service 中直接写大量 SQLAlchemy 查询。

推荐隔离为 Repository：

```text
Application Service
    ↓
Repository Interface
    ↓
SQLAlchemy Repository Implementation
```

### 11.2 推荐结构

```text
shovel/
└── infrastructure/
    └── persistence/
        ├── database.py
        ├── session.py
        ├── models/
        │   ├── agent_model.py
        │   ├── conversation_model.py
        │   └── message_model.py
        └── repositories/
            ├── agent_repository.py
            ├── conversation_repository.py
            └── message_repository.py
```

### 11.3 Repository 设计原则

- Repository 负责数据访问
- Application Service 负责编排业务流程
- Domain Entity 不依赖 SQLAlchemy
- SQLAlchemy Model 不直接泄漏到 API 层
- Repository 返回领域对象或 DTO

---

## 12. Agent Runtime 设计

### 12.1 Agent 的定义

在 Shovel 中，Agent 不只是一个 prompt。

一个 Agent 应该包含：

```text
name
description
instructions
model
tools
memory
knowledge base
runtime settings
session state
```

### 12.2 Agent 实体

```text
Agent
├── name
├── description
├── instructions
├── model
├── tools
├── memory provider
├── knowledge retriever
├── runtime settings
└── session state
```

### 12.3 Agent 运行流程

```text
User input
  ↓
Conversation session
  ↓
Memory context provider
  ↓
Knowledge retriever
  ↓
Tool registry
  ↓
Agent Framework runtime
  ↓
Streaming response
  ↓
Message persistence
```

### 12.4 AgentService 职责

```text
AgentService
├── create_agent()
├── list_agents()
├── get_agent()
├── delete_agent()
├── run_agent()
└── update_agent()
```

### 12.5 Agent Runtime 职责

```text
AgentRuntime
├── load_agent()
├── create_session()
├── invoke()
├── stream()
├── call_tool()
└── persist_result()
```

### 12.6 Agent Session

Agent Session 用于保存一次对话或一次任务运行中的状态。

它应该包括：

```text
session_id
agent_id
conversation_id
message_history
tool_call_history
context
created_at
updated_at
```

---

## 13. Tool 系统设计

### 13.1 Tool 的定位

Tool 是 Agent 与外部能力交互的桥梁。

Shovel 中的 Tool 可以包括：

- 文件系统
- 数据库
- 终端
- Web Search
- Knowledge Search
- Memory
- Email
- Calendar
- MCP Tool
- OpenAPI Tool

### 13.2 Tool 协议

建议定义统一协议：

```python
class Tool(Protocol):
    @property
    def name(self) -> str:
        ...

    async def invoke(
        self,
        request: ToolRequest,
    ) -> ToolResult:
        ...
```

### 13.3 ToolRequest

```text
ToolRequest
├── tool_name
├── arguments
├── context
└── metadata
```

### 13.4 ToolResult

```text
ToolResult
├── success
├── data
├── error
└── metadata
```

### 13.5 Tool Registry

```text
ToolRegistry
├── register()
├── unregister()
├── get()
├── list()
└── invoke()
```

### 13.6 Tool Adapter

不同框架对 Tool 的结构要求可能不同，因此建议增加 adapter。

```text
Shovel Tool Protocol
    ↓
Tool Adapter
    ↓
Agent Framework Tool
```

这样可以避免 Agent Framework 的具体实现污染 Shovel 自己的领域模型。

---

## 14. Knowledge System 设计

### 14.1 Knowledge System 的目标

Knowledge System 用于把文档、笔记、网页、代码、会议资料等内容转化为可检索知识。

它的目标是让 Agent 不仅依赖模型自身知识，而可以基于用户本地或企业知识进行回答。

### 14.2 Document Pipeline

```text
Document input
  ↓
Parser
  ↓
Chunker
  ↓
Embedding
  ↓
Qdrant vector store
  ↓
Retriever
  ↓
Agent context
```

### 14.3 第一阶段支持的文档类型

建议先支持：

```text
Markdown
TXT
PDF
DOCX
```

后续再扩展：

```text
HTML
OneNote
SharePoint
Email
Wiki
Code Repository
```

### 14.4 Knowledge 模块结构

```text
shovel/
└── knowledge/
    ├── document.py
    ├── parser.py
    ├── chunker.py
    ├── embedding.py
    ├── vector_store.py
    └── retriever.py
```

### 14.5 Qdrant 存储内容

```text
chunk_id
document_id
embedding vector
source
metadata
created_at
```

### 14.6 Knowledge CLI

推荐命令：

```bash
shovel kb add ./docs/readme.md
shovel kb search "how to configure shovel"
shovel kb list
shovel kb remove <document-id>
```

---

## 15. Memory System 设计

### 15.1 Memory 不等于 Message History

Message History 是对话记录。

Memory 是从历史中抽取出的长期信息、用户偏好和上下文事实。

### 15.2 Memory 类型

| 类型 | 含义 | 示例 |
|---|---|---|
| Short-term memory | 当前会话上下文 | 本轮对话中的问题、工具结果、临时上下文 |
| Long-term memory | 长期事实和偏好 | 用户偏好、项目目标、常用技术栈 |
| Working memory | 当前任务状态 | workflow 中间结果、待处理任务列表 |

### 15.3 Memory 流程

```text
Conversation
  ↓
Message
  ↓
Memory Extractor
  ↓
Mem0
  ↓
Context Provider
  ↓
Agent Runtime
```

### 15.4 Redis 的定位

Redis 不建议一开始就做复杂。

第一阶段可以用于：

```text
cache
session state
temporary runtime state
task status
```

### 15.5 Memory 模块结构

```text
shovel/
└── memory/
    ├── memory.py
    ├── memory_store.py
    ├── mem0_store.py
    ├── extractor.py
    └── context_provider.py
```

---

## 16. FastAPI Web Host 设计

### 16.1 FastAPI 的定位

FastAPI 用于提供 Shovel 的 Web API。

Web 层应该只做协议适配，不承担核心业务逻辑。

```text
FastAPI Route
  ↓
Application Service
  ↓
Domain / Infrastructure
```

### 16.2 推荐 API

```text
GET    /api/agents
POST   /api/agents
GET    /api/agents/{id}
DELETE /api/agents/{id}

GET    /api/conversations
POST   /api/conversations
GET    /api/conversations/{id}/messages

POST   /api/chat
WS     /ws/chat

GET    /api/settings
PUT    /api/settings

POST   /api/knowledge/documents
GET    /api/knowledge/search
```

### 16.3 WebSocket Chat

未来 Agent Chat 建议支持 streaming：

```text
Browser
  ↓
WebSocket
  ↓
FastAPI
  ↓
Agent Runtime
  ↓
Streaming Response
```

### 16.4 Web 模块结构

```text
shovel/
└── web/
    ├── app.py
    ├── routes/
    │   ├── agents.py
    │   ├── conversations.py
    │   ├── chat.py
    │   ├── knowledge.py
    │   └── settings.py
    ├── schemas/
    └── dependencies.py
```

---

## 17. Next.js Web Console 设计

### 17.1 Web Console 的定位

Web Console 是 Shovel 产品化体验的重要部分。

第一版不需要复杂，但应该具备基础导航、Agent 管理、Chat、Knowledge 和 Settings 页面。

### 17.2 推荐页面

```text
Dashboard
Agents
Conversations
Knowledge
Settings
```

### 17.3 页面职责

| 页面 | 职责 |
|---|---|
| Dashboard | 展示本地 runtime 状态、Agent 数量、最近对话 |
| Agents | 创建、查看、启用和运行 Agent |
| Conversations | 查看历史会话和消息 |
| Knowledge | 上传文档、查看索引状态、执行检索 |
| Settings | 管理模型、数据库、Qdrant、Redis、Memory 配置 |

### 17.4 前端目录结构

```text
frontend/
└── app/
    ├── layout.tsx
    ├── page.tsx
    ├── agents/
    ├── conversations/
    ├── knowledge/
    └── settings/

frontend/
└── components/
    ├── sidebar.tsx
    ├── header.tsx
    ├── agent-card.tsx
    └── chat-panel.tsx
```

### 17.5 前端设计原则

- 页面简单清晰
- 不要过早复杂化状态管理
- 优先实现可用性
- 第一版可以直接调用 FastAPI
- 后续再考虑更复杂的客户端状态和缓存

---

## 18. 推荐目录结构

```text
shovel/
├── src/
│   └── shovel/
│       ├── cli/
│       ├── web/
│       ├── hosting/
│       ├── configuration/
│       ├── logging/
│       ├── dependency_injection/
│       ├── domain/
│       ├── application/
│       ├── infrastructure/
│       ├── agents/
│       ├── memory/
│       ├── knowledge/
│       ├── tools/
│       ├── workflows/
│       ├── telemetry/
│       └── shared/
├── frontend/
│   ├── app/
│   ├── components/
│   ├── hooks/
│   ├── lib/
│   └── styles/
├── migrations/
├── tests/
├── docs/
├── examples/
└── pyproject.toml
```

---

## 19. 分层职责说明

### 19.1 cli

负责：

- 命令定义
- 参数解析
- Rich 输出
- 调用 application service
- 返回 exit code

不负责：

- 直接操作数据库
- 直接创建复杂对象
- 直接调用 Agent Framework

### 19.2 web

负责：

- FastAPI app
- routes
- schemas
- dependencies
- WebSocket

不负责：

- 业务逻辑
- 数据持久化细节

### 19.3 hosting

负责：

- Host
- HostBuilder
- 生命周期
- 启动和关闭
- graceful shutdown

### 19.4 configuration

负责：

- Settings model
- 配置加载
- 默认配置生成
- 配置校验

### 19.5 logging

负责：

- 标准日志初始化
- Rich CLI 输出集成
- 结构化日志扩展
- 后续 telemetry 集成

### 19.6 application

负责：

- 用例编排
- 服务逻辑
- 调用 repository
- 调用 runtime
- 管理业务流程

### 19.7 domain

负责：

- 领域实体
- value objects
- domain rules
- protocol / interface
- 不依赖外部框架

### 19.8 infrastructure

负责：

- SQLAlchemy
- Redis
- Qdrant
- Mem0
- Agent Framework adapter
- 外部系统集成

### 19.9 agents

负责：

- Agent 定义
- Agent Runtime
- Agent Session
- Agent Registry
- Tool Adapter

### 19.10 knowledge

负责：

- Document parsing
- Chunking
- Embedding
- Vector store
- Retrieval

### 19.11 memory

负责：

- Memory extraction
- Memory store
- Context provider
- Long-term memory integration

### 19.12 tools

负责：

- Tool protocol
- Tool registry
- Built-in tools
- Tool execution wrapper

### 19.13 workflows

负责：

- Sequential workflow
- Router workflow
- Orchestrator-worker workflow
- Workflow run tracking

---

## 20. 开发路线图

| 阶段 | 目标 | 核心模块 | 验收标准 |
|---|---|---|---|
| V1 | CLI + Host 基础 | Typer, Rich, Settings, HostBuilder, DI | `shovel init/start/doctor` 可运行 |
| V2 | 本地存储 | SQLAlchemy, Alembic, SQLite, Repository | `agent create/list/run` 具备持久化 |
| V3 | Web API | FastAPI, routes, schemas, WebSocket | 提供 agents/chat/settings API |
| V4 | Agent Runtime | Microsoft Agent Framework, Tool Adapter, Session | 支持创建并运行 Agent |
| V5 | Knowledge + Memory | Qdrant, Mem0, Redis | 支持文档索引、检索和记忆注入 |
| V6 | Web Console | Next.js, CSS, Tailwind | 支持 Dashboard、Agents、Chat、Settings 页面 |
| V7 | Workflow | Workflow runtime, multi-agent orchestration | 支持 sequential/router/orchestrator-worker |
| V8 | Observability | OpenTelemetry, structured logging | 支持运行链路追踪和诊断 |

---

## 21. 学习路线摘要

后续学习建议围绕 Shovel 的真实模块展开，而不是孤立学习框架。

推荐顺序：

1. Typer  
   先完成 CLI command skeleton。

2. Rich  
   完成统一输出系统和错误展示。

3. Pydantic Settings + HostBuilder  
   完成配置和启动生命周期。

4. dependency-injector  
   完成服务注册和对象装配。

5. SQLAlchemy + Alembic  
   完成本地数据库和 migration。

6. FastAPI  
   完成 Web API Host。

7. Microsoft Agent Framework  
   完成 Agent Runtime。

8. Qdrant + Mem0 + Redis  
   完成 Knowledge 和 Memory。

9. Next.js + CSS  
   完成 Web Console。

10. OpenTelemetry + Testing  
    完成可观测性和质量保障。

---

## 22. 关键设计原则

### 22.1 先稳定工程骨架

优先完成：

```text
CLI
Rich output
Configuration
HostBuilder
Dependency Injection
Logging
```

这些是 Shovel 的地基。

### 22.2 再扩展 Agent 能力

Agent Framework、Qdrant、Mem0、Redis、Workflow 都应该在基础架构稳定后再接入。

### 22.3 CLI 和 Web 共用业务逻辑

不要让 CLI 和 Web API 各写一套逻辑。

应该是：

```text
CLI command
    ↓
Application Service

FastAPI route
    ↓
Application Service
```

### 22.4 Infrastructure 可替换

例如：

```text
SQLite -> PostgreSQL
Local Qdrant -> Cloud Qdrant
Local model -> Azure OpenAI
Simple memory -> Mem0
```

因此要避免业务逻辑直接依赖具体实现。

### 22.5 坚持类型安全

建议长期坚持：

```bash
ruff check
ruff format
mypy --strict
pytest
```

### 22.6 保持 Domain 层纯净

Domain 层不应该依赖：

```text
FastAPI
SQLAlchemy
Typer
Rich
Qdrant client
Redis client
Agent Framework SDK
```

Domain 层应该尽量保持为纯 Python 领域模型和协议。

---

## 23. 测试策略

### 23.1 单元测试

覆盖：

- domain model
- application service
- configuration loading
- CLI helper
- tool protocol

### 23.2 集成测试

覆盖：

- repository + SQLite
- FastAPI routes
- AgentService + fake runtime
- KnowledgeService + fake vector store

### 23.3 CLI 测试

覆盖：

```bash
shovel --help
shovel init
shovel doctor
shovel config show
shovel agent list
```

### 23.4 类型检查

建议：

```bash
mypy --strict src/
```

### 23.5 Lint 和格式化

建议：

```bash
ruff check .
ruff format .
```

---

## 24. 打包与发布设计

### 24.1 pyproject.toml

Shovel 应通过 `pyproject.toml` 管理项目。

需要包含：

```text
project metadata
dependencies
optional dependencies
console scripts
tool settings
```

### 24.2 CLI Entry Point

推荐 entry point：

```toml
[project.scripts]
shovel = "shovel.cli.main:main"
```

### 24.3 本地开发命令

推荐：

```bash
uv sync
uv run shovel --help
uv run shovel init
uv run shovel doctor
uv run shovel start
```

### 24.4 本地安装

推荐：

```bash
uv tool install .
```

或者：

```bash
pip install -e .
```

---

## 25. 可观测性设计

### 25.1 Logging

第一阶段可以先使用标准 logging 或 structlog。

需要记录：

- CLI command start/end
- Host startup/shutdown
- configuration load result
- database connection
- agent run
- tool call
- API request
- error and exception

### 25.2 OpenTelemetry

后续加入 OpenTelemetry，用于跟踪：

```text
API request
Agent invocation
Tool execution
Database query
Vector search
Memory lookup
Workflow execution
```

### 25.3 Trace 结构

推荐：

```text
request trace
    ↓
agent run span
    ↓
retrieval span
    ↓
tool call span
    ↓
llm call span
    ↓
persistence span
```

---

## 26. 安全与配置注意事项

### 26.1 敏感配置

以下配置不应该明文提交到 Git：

```text
OpenAI API Key
Azure OpenAI Key
Redis password
Qdrant API Key
Database password
```

### 26.2 推荐做法

- `.env` 不提交
- `settings.example.json` 可以提交
- `settings.json` 默认不提交
- 提供 `shovel init` 生成默认配置
- 支持环境变量覆盖敏感值

### 26.3 日志脱敏

日志中避免输出：

- API key
- connection string password
- token
- personal data
- customer data

---

## 27. 第一阶段最小可行版本

### 27.1 V1 目标

V1 不需要完整 Agent 能力。

V1 的目标是让 Shovel 具备稳定工程骨架。

需要实现：

```text
Typer CLI
Rich output
Pydantic Settings
HostBuilder
Dependency Injection
Logging
shovel init
shovel doctor
shovel start
shovel --version
```

### 27.2 V1 验收标准

命令可运行：

```bash
uv run shovel --version
uv run shovel init
uv run shovel doctor
uv run shovel start
```

目录可生成：

```text
~/.shovel/
├── settings.json
├── data/
├── logs/
└── cache/
```

输出体验清晰：

```text
ℹ Checking default directories.
✓ The default profile directory exists.
✓ The default settings file exists.
✓ Shovel is ready.
```

---

## 28. 第二阶段最小可行版本

### 28.1 V2 目标

V2 重点是数据库和 Agent 元数据管理。

需要实现：

```text
SQLAlchemy models
Alembic migration
AgentRepository
AgentService
shovel agent create
shovel agent list
shovel database migrate
```

### 28.2 V2 验收标准

可运行：

```bash
shovel database migrate
shovel agent create default
shovel agent list
```

SQLite 中应创建：

```text
agents
conversations
messages
settings
```

---

## 29. 第三阶段最小可行版本

### 29.1 V3 目标

V3 重点是 FastAPI Web Host。

需要实现：

```text
FastAPI app
agent routes
conversation routes
settings routes
chat route placeholder
```

### 29.2 V3 验收标准

可访问：

```text
GET /api/agents
POST /api/agents
GET /api/settings
POST /api/chat
```

CLI 可启动 Web Host：

```bash
shovel start
```

---

## 30. 下一步建议

建议接下来优先完成 V1：

```text
Typer
Rich
HostBuilder
Pydantic Settings
dependency-injector
Logging
```

推荐最近实现顺序：

```text
1. shovel.cli.app
2. shovel.cli.console
3. shovel.configuration.settings
4. shovel.hosting.host_builder
5. shovel.dependency_injection.container
6. shovel commands: init, doctor, start, version
```

完成这些后，Shovel 的主干架构会稳定下来。后续接入 SQLAlchemy、FastAPI、Agent Framework、Qdrant、Mem0 和 Next.js 会更加自然，也不容易导致目录结构和依赖关系混乱。

---

## 31. 总结

Shovel 的核心价值不在于单个技术点，而在于把这些技术组合成一个完整、清晰、可扩展的 Agent Platform。

它的设计核心可以概括为：

```text
Generic Host 负责启动和生命周期
Configuration 负责配置
DI 负责对象装配
CLI 和 Web 负责入口
Application 负责业务用例
Domain 负责核心模型
Infrastructure 负责外部实现
Agent Runtime 负责智能体执行
Knowledge 负责外部知识
Memory 负责长期上下文
Tool 负责外部能力
Workflow 负责任务编排
Observability 负责诊断和追踪
Testing 负责质量保障
```

最终，Shovel 应该成为一个具备良好工程结构、产品体验和长期演进能力的个人 AI Agent 平台。
