# Shovel 架构与设计说明文档

## 文档信息

| 项目 | 内容 |
|---|---|
| 文档名称 | Shovel 架构与设计说明文档 |
| 项目名称 | Shovel AI Agent Studio and Runtime Platform |
| 文档类型 | 架构设计说明 / 产品设计说明 / 技术设计说明 |
| 作者 | HongWei Guo |
| 版本 | Draft v2.0 |
| 推荐路径 | `docs/shovel-architecture-design.md` |
| 更新时间 | 2026/7/31 |
| 目标读者 | 项目开发者、架构设计者、后续维护者 |

---

## 1. 执行摘要

Shovel 是一个受 **Azure Data Factory** 设计理念启发的个人 AI Agent Studio 与 Runtime 平台。

它不仅是一个类似 OpenClaw 的个人 AI Agent 工具，也不仅是一个简单的 Chat UI、Prompt 管理器或 RAG 应用。Shovel 的目标是成为一个面向个人开发者的 **AI Agent Factory**，帮助用户通过统一的 GUI 和 CLI 创建、测试、发布和运行 Prompt、Skill、Agent、Workflow、Knowledge Base 和 Memory。

Shovel 的核心设计思想是将 **Studio** 与 **Runtime** 分离：

```text
Shovel
=
  Shovel Studio
+ Shovel Runtime
```

类似：

```text
Azure Data Factory
=
  ADF Studio
+ Integration Runtime
```

其中：

- **Shovel Studio** 负责可视化设计、开发、测试、验证和发布。
- **Shovel Runtime** 负责执行 Prompt、Skill、Agent、Workflow 和 Trigger。

Shovel 的长期目标是让个人开发者可以像使用 Azure Data Factory 构建数据管道一样，构建和运行自己的 AI Agent 工作流系统。

---

## 2. 产品定位

### 2.1 Shovel 是什么

Shovel 是一个个人 AI Agent Studio 与 Runtime 平台。

它是：

- Prompt 开发和发布工具
- Skill 开发、测试和验证工具
- Workflow 创建、调试和测试工具
- Knowledge Base 管理工具
- Agent Memory 管理工具
- Agent 创建和运行平台
- 面向个人开发者的 AI Agent Factory

可以概括为：

```text
Shovel
=
  Prompt Studio
+ Skill Studio
+ Workflow Studio
+ Agent Studio
+ Knowledge Studio
+ Memory Studio
+ Runtime Host
```

### 2.2 Shovel 与 OpenClaw 的关系

Shovel 的定位接近 OpenClaw 这类个人 AI Agent 工具，但 Shovel 不只是一个 Agent 产品。

Shovel 相比类似工具的不同点在于：

1. 它不仅运行 Agent，还负责开发和管理 Prompt。
2. 它不仅调用工具，还支持 Skill 的开发、测试、验证和发布。
3. 它不仅提供 Agent Chat，还提供 Workflow 创建、调试和运行。
4. 它不仅管理知识库，还管理 Agent 的长期记忆。
5. 它借鉴 Azure Data Factory 的设计理念，将 Studio 与 Runtime 分开。
6. 它将模型、向量数据库、记忆、数据源、工具和 Trigger 抽象成可配置资源。
7. 它通过 GUI 创建 Prompt、Skill、Agent、Workflow、Knowledge 和 Memory。

因此，Shovel 更准确的定位是：

```text
Personal AI Agent Factory
```

或者：

```text
AI Agent Studio and Runtime Platform
```

### 2.3 Shovel 不是什么

Shovel 不应该被设计成：

- 单纯的 Chat UI
- 单纯的 Prompt 管理工具
- 单纯的 RAG 工具
- 单纯的 Workflow 工具
- 单纯的 Agent Framework Wrapper
- 单纯的 Knowledge Base 管理器
- 单纯的本地向量数据库 UI

这些能力都可以是 Shovel 的一部分，但不能代表 Shovel 的整体定位。

### 2.4 Shovel 应该是什么

Shovel 应该是一个完整的个人 AI Agent 平台。

它应该让用户能够：

- 创建 Prompt
- 测试 Prompt
- 发布 Prompt
- 创建 Skill
- 测试 Skill
- 验证 Skill
- 创建 Agent
- 配置 Agent 使用的模型、知识库和记忆
- 创建 Workflow
- 编排 Prompt、Skill、Agent 和 Tool
- 管理 Knowledge Source
- 管理 Agent Memory
- 通过 Trigger 自动运行 Workflow
- 通过 Studio 可视化管理所有对象
- 通过 Runtime 执行所有定义好的对象

---

## 3. 核心设计理念

### 3.1 Studio 与 Runtime 分离

Shovel 的核心设计是分离：

```text
Studio
Runtime
```

Studio 负责设计和管理。

Runtime 负责执行和运行。

这种模式借鉴 Azure Data Factory：

```text
ADF Studio
    负责创建 Linked Service、Dataset、Pipeline、Activity、Trigger

ADF Integration Runtime
    负责实际执行数据移动和计算任务
```

Shovel 中对应为：

```text
Shovel Studio
    负责创建 Resource、Prompt、Skill、Agent、Workflow、Trigger

Shovel Runtime
    负责执行 Prompt、Skill、Agent、Workflow、Trigger
```

### 3.2 ADF 风格的抽象模型

Azure Data Factory 的核心概念包括：

```text
Linked Service
Dataset
Activity
Pipeline
Trigger
Integration Runtime
```

Shovel 可以借鉴并映射为：

| ADF 概念 | Shovel 概念 | 含义 |
|---|---|---|
| Linked Service | Resource / Connection | 外部资源连接，例如模型、数据库、向量库、API |
| Dataset | Knowledge Source / Data Source | 可被读取或索引的数据来源 |
| Activity | Activity | Workflow 中的最小执行单元 |
| Pipeline | Workflow | 由多个 Activity 组成的工作流 |
| Trigger | Trigger | 触发 Workflow 执行的机制 |
| Integration Runtime | Shovel Runtime | 实际执行 Prompt、Skill、Agent、Workflow 的运行时 |

### 3.3 一切都是可设计资源

Shovel 中的对象都应该可以在 Studio 中被创建、编辑、测试、验证和发布。

包括：

```text
Resource
Prompt
Skill
Agent
Workflow
Knowledge Source
Memory Store
Trigger
Runtime
```

### 3.4 本地优先，未来可扩展到云端

Shovel 第一阶段应该以本地个人使用为主：

```text
Local SQLite
Local Settings
Local Runtime
Local Qdrant
Local Redis
Local Knowledge
Local Agent
```

未来可以扩展为：

```text
Cloud Runtime
Remote Vector Database
Remote Memory Store
Remote Agent Registry
Team Workspace
Shared Prompt Library
Shared Skill Library
```

---

## 4. 总体架构

### 4.1 高层架构

```text
┌──────────────────────────────────────────────┐
│                Shovel Studio                 │
├──────────────────────────────────────────────┤
│ Prompt Studio                                │
│ Skill Studio                                 │
│ Agent Studio                                 │
│ Workflow Studio                              │
│ Knowledge Studio                             │
│ Memory Studio                                │
│ Resource Studio                              │
│ Trigger Studio                               │
└──────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│                 Shovel API                   │
├──────────────────────────────────────────────┤
│ REST API                                     │
│ WebSocket API                                │
│ Studio Management API                        │
│ Runtime Control API                          │
└──────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│                Shovel Runtime                │
├──────────────────────────────────────────────┤
│ Prompt Runtime                               │
│ Skill Runtime                                │
│ Agent Runtime                                │
│ Workflow Runtime                             │
│ Trigger Runtime                              │
│ Tool Runtime                                 │
└──────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│                 Resources                    │
├──────────────────────────────────────────────┤
│ Model Providers                              │
│ Vector Databases                             │
│ Memory Stores                                │
│ SQL Databases                                │
│ File Systems                                 │
│ APIs                                         │
│ MCP Servers                                  │
│ External Services                            │
└──────────────────────────────────────────────┘
```

### 4.2 工程分层架构

```text
┌──────────────────────────────────────────────┐
│ Presentation Layer                            │
├──────────────────────────────────────────────┤
│ CLI: Typer + Rich                             │
│ Web UI: Next.js + CSS/Tailwind                │
│ Web API: FastAPI                              │
└──────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│ Application Layer                             │
├──────────────────────────────────────────────┤
│ PromptService                                 │
│ SkillService                                  │
│ AgentService                                  │
│ WorkflowService                               │
│ KnowledgeService                              │
│ MemoryService                                 │
│ ResourceService                               │
│ TriggerService                                │
│ RuntimeService                                │
└──────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│ Domain Layer                                  │
├──────────────────────────────────────────────┤
│ Prompt                                        │
│ Skill                                         │
│ Agent                                         │
│ Workflow                                      │
│ Activity                                      │
│ Trigger                                       │
│ Resource                                      │
│ KnowledgeSource                               │
│ MemoryStore                                   │
│ RuntimeDefinition                             │
└──────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│ Infrastructure Layer                          │
├──────────────────────────────────────────────┤
│ SQLAlchemy / Alembic                          │
│ SQLite / PostgreSQL                           │
│ Qdrant                                        │
│ Redis                                         │
│ Mem0                                          │
│ Microsoft Agent Framework                     │
│ OpenTelemetry                                 │
│ Dependency Injector                           │
│ External API Clients                          │
└──────────────────────────────────────────────┘
```

---

## 5. 核心产品模块

### 5.1 Shovel Studio

Shovel Studio 是用户进行可视化设计和管理的地方。

它包括：

```text
Prompt Studio
Skill Studio
Agent Studio
Workflow Studio
Knowledge Studio
Memory Studio
Resource Studio
Trigger Studio
```

Shovel Studio 的目标是让用户不用直接写大量配置文件，也能通过 GUI 创建和管理 Agent 系统。

### 5.2 Shovel Runtime

Shovel Runtime 是实际执行系统。

它负责：

```text
Execute Prompt
Execute Skill
Execute Agent
Execute Workflow
Execute Trigger
Execute Tool
Retrieve Knowledge
Retrieve Memory
Persist Execution Result
```

Runtime 不负责设计界面。

Runtime 只负责执行已经定义好的对象。

### 5.3 Shovel CLI

CLI 是开发者入口。

它负责：

```text
shovel init
shovel start
shovel doctor
shovel prompt
shovel skill
shovel agent
shovel workflow
shovel kb
shovel memory
shovel resource
```

CLI 适合：

- 初始化项目
- 启动 Runtime
- 检查环境
- 执行本地测试
- 快速运行 Agent
- 自动化操作

### 5.4 Shovel Web API

Web API 是 Studio 与 Runtime 的桥梁。

它提供：

```text
Studio Management API
Runtime Control API
Agent API
Workflow API
Prompt API
Skill API
Knowledge API
Memory API
Resource API
```

### 5.5 Shovel Web Console

Web Console 基于 Next.js 实现。

它是 Studio 的主要形式。

---

## 6. 核心领域对象

### 6.1 Resource

Resource 类似 Azure Data Factory 中的 Linked Service。

它表示 Shovel 可以连接和使用的外部或本地资源。

#### Resource 类型

```text
Model Provider
Vector Database
Memory Store
SQL Database
File System
REST API
MCP Server
Storage
Search Engine
```

#### Resource 示例

```text
Azure OpenAI
OpenAI
Ollama
Qdrant
Redis
SQLite
PostgreSQL
Cosmos DB
Local File System
REST API Endpoint
MCP Server
```

#### Resource 定义

```text
Resource
├── id
├── name
├── type
├── provider
├── connection
├── credentials
├── metadata
├── created_at
└── updated_at
```

#### 设计原则

- Resource 只描述连接信息和能力。
- Resource 不直接代表业务逻辑。
- Prompt、Skill、Agent、Workflow 可以引用 Resource。
- Resource 的敏感信息必须支持加密或环境变量引用。

---

### 6.2 Prompt

Prompt 是 Shovel 的一等公民。

Shovel 不只是运行 Prompt，也应该支持 Prompt 的开发、测试、版本管理和发布。

#### Prompt 用途

```text
Prompt 开发
Prompt 调试
Prompt 测试
Prompt 评估
Prompt 版本管理
Prompt 发布
Prompt 复用
```

#### Prompt 定义

```text
Prompt
├── id
├── name
├── description
├── system_prompt
├── user_template
├── variables
├── examples
├── evaluation
├── version
├── status
├── metadata
├── created_at
└── updated_at
```

#### Prompt 状态

```text
draft
testing
published
deprecated
archived
```

#### Prompt Studio 能力

```text
Create Prompt
Edit Prompt
Test Prompt
Compare Prompt Versions
Publish Prompt
Rollback Prompt
Evaluate Prompt
```

---

### 6.3 Skill

Skill 是 Shovel 中可复用的能力单元。

它可以是代码能力、Prompt 能力、API 能力、MCP 能力或 Workflow 能力。

#### Skill 类型

```text
Python Skill
Prompt Skill
REST Skill
MCP Skill
Workflow Skill
Tool Skill
```

#### Skill 定义

```text
Skill
├── id
├── name
├── description
├── type
├── inputs
├── outputs
├── implementation
├── dependencies
├── tests
├── validation
├── version
├── status
├── metadata
├── created_at
└── updated_at
```

#### Skill 生命周期

```text
draft
testing
validated
published
deprecated
archived
```

#### Skill Studio 能力

```text
Create Skill
Edit Skill
Run Skill
Test Skill
Validate Skill
Publish Skill
Version Skill
```

#### Skill 与 Tool 的区别

Tool 更偏运行时调用接口。

Skill 更偏可开发、可测试、可发布的能力单元。

可以理解为：

```text
Tool
    是底层可调用能力

Skill
    是面向 Agent 和 Workflow 的可复用能力封装
```

---

### 6.4 Agent

Agent 是由 Prompt、Skill、Knowledge、Memory、Model 和 Runtime Configuration 组合而成的智能体。

#### Agent 定义

```text
Agent
├── id
├── name
├── description
├── model_resource
├── prompt
├── skills
├── knowledge_sources
├── memory_store
├── workflow
├── runtime_config
├── version
├── status
├── metadata
├── created_at
└── updated_at
```

#### Agent 不应该只是 Prompt

Agent 不应该等同于：

```text
model + system prompt
```

它应该是：

```text
Agent
=
  Model
+ Prompt
+ Skills
+ Knowledge
+ Memory
+ Runtime Configuration
+ Optional Workflow
```

#### Agent Studio 能力

```text
Create Agent
Configure Model
Attach Prompt
Attach Skills
Attach Knowledge
Attach Memory
Test Agent
Publish Agent
Run Agent
Inspect Agent Runs
```

---

### 6.5 Workflow

Workflow 是 Shovel 中用于编排 Prompt、Skill、Agent、Tool、Knowledge 和 Memory 的核心对象。

它类似 Azure Data Factory 中的 Pipeline。

#### Workflow 定义

```text
Workflow
├── id
├── name
├── description
├── activities
├── variables
├── parameters
├── conditions
├── outputs
├── triggers
├── version
├── status
├── metadata
├── created_at
└── updated_at
```

#### Workflow Studio 能力

```text
Create Workflow
Edit Workflow
Drag and Drop Activities
Configure Activity Inputs
Configure Activity Outputs
Test Workflow
Debug Workflow
Publish Workflow
Run Workflow
View Run History
```

#### Workflow 状态

```text
draft
testing
published
disabled
archived
```

---

### 6.6 Activity

Activity 是 Workflow 中的最小执行单元。

它类似 ADF Activity。

#### Activity 类型

```text
Prompt Activity
Skill Activity
Agent Activity
Tool Activity
Knowledge Activity
Memory Activity
Condition Activity
Loop Activity
Code Activity
HTTP Activity
MCP Activity
```

#### Activity 定义

```text
Activity
├── id
├── name
├── type
├── inputs
├── outputs
├── depends_on
├── retry_policy
├── timeout
├── condition
├── metadata
└── runtime_config
```

#### Activity 执行原则

- Activity 应该有明确输入。
- Activity 应该有明确输出。
- Activity 应该支持失败重试。
- Activity 应该支持条件执行。
- Activity 应该支持运行记录。
- Activity 的执行结果应该可被后续 Activity 引用。

---

### 6.7 Trigger

Trigger 用于触发 Workflow 或 Agent 运行。

#### Trigger 类型

```text
Manual Trigger
Schedule Trigger
Webhook Trigger
Event Trigger
File Trigger
Email Trigger
API Trigger
```

#### Trigger 定义

```text
Trigger
├── id
├── name
├── type
├── target
├── schedule
├── event_condition
├── enabled
├── metadata
├── created_at
└── updated_at
```

#### Trigger Studio 能力

```text
Create Trigger
Enable Trigger
Disable Trigger
Test Trigger
View Trigger History
```

---

### 6.8 Knowledge Source

Knowledge Source 表示可被 Shovel 索引和检索的知识来源。

#### Knowledge Source 类型

```text
Markdown
TXT
PDF
DOCX
HTML
Website
Local Folder
Git Repository
SharePoint
OneNote
Email
Wiki
Database
```

#### Knowledge Pipeline

```text
Knowledge Source
    ↓
Parser
    ↓
Chunker
    ↓
Embedding
    ↓
Vector Store
    ↓
Retriever
    ↓
Agent Context
```

#### Knowledge Source 定义

```text
KnowledgeSource
├── id
├── name
├── type
├── location
├── parser_config
├── chunking_config
├── embedding_resource
├── vector_store_resource
├── index_status
├── metadata
├── created_at
└── updated_at
```

---

### 6.9 Memory Store

Memory Store 用于管理 Agent 的长期记忆。

#### Memory 类型

```text
Short-term Memory
Long-term Memory
Working Memory
User Memory
Agent Memory
Workflow Memory
```

#### Memory Store 实现

```text
Mem0
Redis
Vector Memory
Graph Memory
SQL Memory
```

#### Memory Store 定义

```text
MemoryStore
├── id
├── name
├── type
├── provider
├── connection_resource
├── retention_policy
├── retrieval_policy
├── metadata
├── created_at
└── updated_at
```

---

## 7. Runtime 设计

### 7.1 Runtime 的职责

Shovel Runtime 负责执行 Studio 中定义的对象。

Runtime 主要职责：

```text
Load Definition
Resolve Resources
Validate Inputs
Execute Activity
Execute Prompt
Execute Skill
Execute Agent
Execute Workflow
Handle Trigger
Persist Run History
Emit Logs and Traces
Return Outputs
```

### 7.2 Runtime 架构

```text
Shovel Runtime
├── Prompt Runtime
├── Skill Runtime
├── Agent Runtime
├── Workflow Runtime
├── Trigger Runtime
├── Tool Runtime
├── Knowledge Runtime
├── Memory Runtime
└── Observability Runtime
```

### 7.3 Prompt Runtime

负责：

```text
Render Prompt Template
Validate Variables
Call Model Provider
Return Model Response
Record Prompt Run
```

### 7.4 Skill Runtime

负责：

```text
Load Skill Definition
Validate Inputs
Execute Skill Implementation
Validate Outputs
Record Skill Run
```

### 7.5 Agent Runtime

负责：

```text
Load Agent Definition
Resolve Model Resource
Resolve Prompt
Resolve Skills
Resolve Knowledge
Resolve Memory
Create Agent Session
Run Agent
Persist Messages
Record Agent Run
```

### 7.6 Workflow Runtime

负责：

```text
Load Workflow Definition
Build Activity Graph
Resolve Dependencies
Execute Activities
Manage Variables
Manage Conditions
Handle Retry
Handle Failure
Persist Workflow Run
```

### 7.7 Trigger Runtime

负责：

```text
Load Trigger
Evaluate Trigger Condition
Start Workflow Run
Start Agent Run
Record Trigger Event
```

---

## 8. Studio 设计

### 8.1 Studio 总体设计

Studio 是 Shovel 的 GUI 设计环境。

建议使用：

```text
Next.js
React
Tailwind CSS
FastAPI
WebSocket
```

Studio 中的核心页面：

```text
Dashboard
Resources
Prompts
Skills
Agents
Workflows
Knowledge
Memory
Triggers
Runs
Settings
```

### 8.2 Dashboard

Dashboard 用于显示系统状态：

```text
Runtime Status
Recent Runs
Agent Count
Workflow Count
Prompt Count
Skill Count
Knowledge Index Status
Memory Status
Errors and Warnings
```

### 8.3 Resource Studio

用于创建和管理外部资源连接：

```text
Model Provider
Vector Database
Memory Store
SQL Database
API
MCP Server
File System
```

### 8.4 Prompt Studio

用于开发和测试 Prompt：

```text
Prompt Editor
Variable Editor
Test Console
Evaluation Result
Version History
Publish Action
```

### 8.5 Skill Studio

用于开发和验证 Skill：

```text
Skill Editor
Input Schema Editor
Output Schema Editor
Test Runner
Validation Result
Dependency Viewer
Publish Action
```

### 8.6 Agent Studio

用于创建 Agent：

```text
Agent Profile
Model Selection
Prompt Selection
Skill Attachment
Knowledge Attachment
Memory Attachment
Runtime Config
Test Chat
Publish Action
```

### 8.7 Workflow Studio

用于可视化创建 Workflow：

```text
Canvas
Activity Palette
Activity Config Panel
Variable Panel
Output Panel
Debug Console
Run History
```

### 8.8 Knowledge Studio

用于管理知识库：

```text
Knowledge Source List
Document Upload
Index Status
Chunk Viewer
Retrieval Test
Embedding Config
Vector Store Config
```

### 8.9 Memory Studio

用于管理记忆：

```text
Memory Store Config
Memory List
Memory Search
Memory Delete
Memory Evaluation
Memory Injection Preview
```

### 8.10 Runs 页面

用于查看运行历史：

```text
Prompt Runs
Skill Runs
Agent Runs
Workflow Runs
Trigger Runs
Tool Calls
Errors
Traces
```

---

## 9. CLI 设计

### 9.1 CLI 的定位

CLI 是 Shovel 的开发者入口。

它可以用于：

```text
Initialize Workspace
Start Runtime
Run Doctor Check
Manage Resources
Manage Prompts
Manage Skills
Manage Agents
Manage Workflows
Run Tests
Run Agent
Run Workflow
```

### 9.2 推荐命令

```text
shovel --version

shovel init
shovel doctor
shovel start

shovel resource list
shovel resource create
shovel resource test

shovel prompt list
shovel prompt test
shovel prompt publish

shovel skill list
shovel skill test
shovel skill validate
shovel skill publish

shovel agent list
shovel agent create
shovel agent run
shovel agent publish

shovel workflow list
shovel workflow run
shovel workflow test
shovel workflow publish

shovel kb add
shovel kb search
shovel kb list

shovel memory list
shovel memory search

shovel trigger list
shovel trigger enable
shovel trigger disable
```

### 9.3 CLI 输出规范

使用 Rich 封装统一输出：

```text
info
success
warning
error
debug
table
panel
status
```

示例：

```text
ℹ Checking Shovel workspace.
✓ Settings file found.
✓ SQLite database ready.
✓ Runtime dependencies ready.
⚠ Qdrant is not configured.
✗ Failed to connect to model provider.
```

---

## 10. Web API 设计

### 10.1 API 模块

```text
/api/resources
/api/prompts
/api/skills
/api/agents
/api/workflows
/api/activities
/api/triggers
/api/knowledge
/api/memory
/api/runs
/api/settings
```

### 10.2 Runtime API

```text
POST /api/runtime/prompts/{id}/run
POST /api/runtime/skills/{id}/run
POST /api/runtime/agents/{id}/run
POST /api/runtime/workflows/{id}/run
POST /api/runtime/triggers/{id}/test
```

### 10.3 Studio API

```text
GET    /api/prompts
POST   /api/prompts
GET    /api/prompts/{id}
PUT    /api/prompts/{id}
DELETE /api/prompts/{id}

GET    /api/skills
POST   /api/skills
GET    /api/skills/{id}
PUT    /api/skills/{id}
DELETE /api/skills/{id}
```

### 10.4 Runs API

```text
GET /api/runs
GET /api/runs/{id}
GET /api/runs/{id}/logs
GET /api/runs/{id}/trace
```

---

## 11. 数据与持久化设计

### 11.1 数据库选择

第一阶段使用：

```text
SQLite
```

后续可以扩展到：

```text
PostgreSQL
SQL Server
MySQL
```

### 11.2 ORM

使用：

```text
SQLAlchemy 2.0
```

### 11.3 Migration

使用：

```text
Alembic
```

### 11.4 核心表

```text
resources
prompts
prompt_versions
skills
skill_versions
agents
agent_versions
workflows
workflow_versions
activities
triggers
knowledge_sources
documents
chunks
memory_stores
memories
runs
run_steps
tool_calls
settings
```

### 11.5 Run History

所有 Runtime 执行都应该产生运行记录。

```text
Run
├── id
├── type
├── target_id
├── status
├── inputs
├── outputs
├── error
├── started_at
├── ended_at
└── metadata
```

Run 类型包括：

```text
prompt_run
skill_run
agent_run
workflow_run
trigger_run
tool_call
```

---

## 12. Knowledge 架构

### 12.1 Knowledge 目标

Knowledge 模块负责把各类知识来源转化为 Agent 可检索上下文。

### 12.2 Pipeline

```text
Source
  ↓
Parser
  ↓
Chunker
  ↓
Embedding
  ↓
Vector Store
  ↓
Retriever
  ↓
Agent Context
```

### 12.3 支持来源

```text
Markdown
PDF
DOCX
TXT
HTML
Website
Local Folder
Git Repository
SharePoint
OneNote
Email
Database
```

### 12.4 Knowledge 与 Resource 的关系

Knowledge Source 不直接持有底层连接。

它应该引用 Resource。

例如：

```text
Knowledge Source: My Docs
    source_resource: Local File System
    embedding_resource: Azure OpenAI Embedding
    vector_store_resource: Qdrant
```

---

## 13. Memory 架构

### 13.1 Memory 目标

Memory 模块负责管理 Agent 的长期上下文和历史经验。

### 13.2 Memory 类型

| 类型 | 含义 |
|---|---|
| Short-term Memory | 当前会话上下文 |
| Long-term Memory | 长期用户偏好和事实 |
| Working Memory | 当前任务执行状态 |
| Agent Memory | 특정 Agent 的历史记忆 |
| Workflow Memory | Workflow 执行中的状态记忆 |

### 13.3 Memory Runtime

```text
Message
  ↓
Memory Extractor
  ↓
Memory Store
  ↓
Memory Retriever
  ↓
Agent Context Provider
```

### 13.4 Memory Store

支持：

```text
Mem0
Redis
Vector Store
SQL Store
Graph Store
```

---

## 14. Workflow 架构

### 14.1 Workflow 的定位

Workflow 是 Shovel 的核心能力之一。

它类似 ADF Pipeline，但面向 AI Agent 场景。

### 14.2 Workflow 组成

```text
Workflow
├── Parameters
├── Variables
├── Activities
├── Conditions
├── Outputs
├── Triggers
└── Runtime Config
```

### 14.3 Activity 编排

Activity 之间可以通过依赖关系连接：

```text
Activity A
    ↓
Activity B
    ↓
Activity C
```

也可以支持条件：

```text
Activity A
    ↓
if success
    ├── Activity B
    └── Activity C
```

### 14.4 Workflow 示例

```text
User Uploads Document
    ↓
Parse Document
    ↓
Create Chunks
    ↓
Generate Embeddings
    ↓
Store in Qdrant
    ↓
Update Knowledge Index
```

另一个示例：

```text
User Question
    ↓
Retrieve Knowledge
    ↓
Retrieve Memory
    ↓
Run Prompt
    ↓
Call Skill if needed
    ↓
Generate Answer
    ↓
Store Conversation
```

---

## 15. Prompt 架构

### 15.1 Prompt 模型

```text
Prompt
├── Name
├── Description
├── System Prompt
├── User Template
├── Variables
├── Examples
├── Evaluation
├── Version
└── Status
```

### 15.2 Prompt 变量

Prompt 应支持变量：

```text
{{user_input}}
{{context}}
{{memory}}
{{knowledge}}
{{tools}}
```

### 15.3 Prompt 测试

Prompt Studio 应支持：

```text
输入测试变量
运行测试
查看模型输出
比较不同版本
记录测试结果
```

### 15.4 Prompt 发布

Prompt 应支持：

```text
draft
testing
published
deprecated
archived
```

---

## 16. Skill 架构

### 16.1 Skill 模型

```text
Skill
├── Name
├── Description
├── Type
├── Input Schema
├── Output Schema
├── Implementation
├── Tests
├── Validation
├── Version
└── Status
```

### 16.2 Skill 类型

```text
Python Skill
Prompt Skill
REST Skill
MCP Skill
Workflow Skill
```

### 16.3 Skill 测试

Skill Studio 应支持：

```text
输入测试数据
执行 Skill
查看输出
校验 Output Schema
查看日志
保存测试用例
```

### 16.4 Skill 发布

Skill 应该支持：

```text
draft
testing
validated
published
deprecated
archived
```

---

## 17. Agent 架构

### 17.1 Agent 模型

```text
Agent
├── Name
├── Description
├── Model Resource
├── Prompt
├── Skills
├── Knowledge Sources
├── Memory Store
├── Runtime Config
├── Version
└── Status
```

### 17.2 Agent 运行流程

```text
User Input
  ↓
Load Agent
  ↓
Resolve Model
  ↓
Render Prompt
  ↓
Retrieve Knowledge
  ↓
Retrieve Memory
  ↓
Execute Agent Runtime
  ↓
Call Skills if needed
  ↓
Return Response
  ↓
Persist Conversation
```

### 17.3 Agent Studio

Agent Studio 需要支持：

```text
创建 Agent
选择模型
选择 Prompt
绑定 Skill
绑定 Knowledge Source
绑定 Memory Store
测试 Agent
发布 Agent
查看运行历史
```

---

## 18. Resource 架构

### 18.1 Resource 的定位

Resource 是所有外部连接和基础能力的统一抽象。

它类似 ADF Linked Service。

### 18.2 Resource 类型

```text
Model Provider
Embedding Provider
Vector Database
Memory Store
SQL Database
File System
REST API
MCP Server
Storage
Search Service
```

### 18.3 Resource 示例

```text
Azure OpenAI Chat Model
Azure OpenAI Embedding Model
OpenAI Model
Ollama Local Model
Qdrant Local
Qdrant Cloud
Redis Local
SQLite
PostgreSQL
Local File System
MCP Server
REST API Endpoint
```

### 18.4 Resource Studio

Resource Studio 应支持：

```text
创建 Resource
测试连接
更新 Resource
禁用 Resource
删除 Resource
查看依赖关系
```

---

## 19. Trigger 架构

### 19.1 Trigger 的定位

Trigger 用来自动启动 Workflow 或 Agent。

### 19.2 Trigger 类型

```text
Manual Trigger
Schedule Trigger
Webhook Trigger
File Trigger
Event Trigger
Email Trigger
API Trigger
```

### 19.3 Trigger 执行流程

```text
Trigger Event
  ↓
Evaluate Condition
  ↓
Resolve Target
  ↓
Start Workflow or Agent
  ↓
Record Trigger Run
```

---

## 20. Hosting 与 Generic Host

### 20.1 为什么需要 Generic Host

Shovel 需要统一管理：

```text
Configuration
Logging
Dependency Injection
Database
Runtime
Web Server
Background Services
Resource Lifecycle
```

因此需要 HostBuilder。

### 20.2 HostBuilder

```text
HostBuilder
├── configure_configuration()
├── configure_logging()
├── configure_services()
├── configure_runtime()
├── configure_web_host()
└── build()
```

### 20.3 Host

```text
Host
├── start()
├── run()
├── stop()
└── dispose()
```

### 20.4 启动流程

```text
CLI command
  ↓
Create HostBuilder
  ↓
Load Configuration
  ↓
Configure Logging
  ↓
Configure DI Container
  ↓
Configure Database
  ↓
Configure Runtime
  ↓
Configure Web Host
  ↓
Build Host
  ↓
Run Host
```

---

## 21. 配置系统

### 21.1 配置来源

```text
Default Settings
settings.json
.env
Environment Variables
CLI Options
```

### 21.2 配置优先级

```text
1. CLI Options
2. Environment Variables
3. .env
4. settings.json
5. Default Values
```

### 21.3 配置文件路径

```text
~/.shovel/settings.json
~/.shovel/data/
~/.shovel/logs/
~/.shovel/cache/
```

### 21.4 配置模型

```text
AppSettings
DatabaseSettings
WebSettings
RuntimeSettings
ModelSettings
QdrantSettings
RedisSettings
MemorySettings
TelemetrySettings
```

---

## 22. 依赖注入

### 22.1 Container

```text
Container
├── settings
├── logger
├── database_engine
├── session_factory
├── repositories
├── services
├── runtimes
├── resource_clients
└── web_app
```

### 22.2 设计原则

- CLI 不直接创建复杂服务。
- Web API 不直接初始化数据库。
- Application Service 不直接依赖具体 SDK。
- Infrastructure 实现可以替换。
- 测试可以注入 Fake Repository 或 Fake Runtime。

---

## 23. 推荐目录结构

```text
shovel/
├── src/
│   └── shovel/
│       ├── cli/
│       ├── web/
│       ├── studio/
│       ├── hosting/
│       ├── configuration/
│       ├── logging/
│       ├── dependency_injection/
│       ├── domain/
│       │   ├── resources/
│       │   ├── prompts/
│       │   ├── skills/
│       │   ├── agents/
│       │   ├── workflows/
│       │   ├── knowledge/
│       │   ├── memory/
│       │   └── triggers/
│       ├── application/
│       │   ├── services/
│       │   └── use_cases/
│       ├── infrastructure/
│       │   ├── persistence/
│       │   ├── vectorstores/
│       │   ├── memory/
│       │   ├── model_providers/
│       │   ├── resources/
│       │   └── telemetry/
│       ├── runtime/
│       │   ├── prompt_runtime/
│       │   ├── skill_runtime/
│       │   ├── agent_runtime/
│       │   ├── workflow_runtime/
│       │   ├── trigger_runtime/
│       │   └── tool_runtime/
│       ├── tools/
│       ├── shared/
│       └── telemetry/
├── frontend/
│   ├── app/
│   ├── components/
│   ├── features/
│   │   ├── resources/
│   │   ├── prompts/
│   │   ├── skills/
│   │   ├── agents/
│   │   ├── workflows/
│   │   ├── knowledge/
│   │   ├── memory/
│   │   └── runs/
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

## 24. 数据库模型建议

### 24.1 核心实体表

```text
resources
prompts
prompt_versions
skills
skill_versions
agents
agent_versions
workflows
workflow_versions
activities
triggers
knowledge_sources
documents
chunks
memory_stores
memories
runs
run_steps
tool_calls
settings
```

### 24.2 运行记录表

```text
runs
run_steps
run_logs
tool_calls
workflow_activity_runs
```

### 24.3 版本管理表

```text
prompt_versions
skill_versions
agent_versions
workflow_versions
```

---

## 25. 开发路线图

### 25.1 V1: CLI + Host + Config

目标：

```text
shovel init
shovel doctor
shovel start
```

包含：

```text
Typer
Rich
Pydantic Settings
HostBuilder
Dependency Injector
Logging
```

### 25.2 V2: Local Persistence

目标：

```text
SQLite
SQLAlchemy
Alembic
Repository
```

实现：

```text
resources
prompts
skills
agents
workflows
settings
```

### 25.3 V3: Studio API

目标：

```text
FastAPI
REST API
Studio Management API
```

实现：

```text
/api/resources
/api/prompts
/api/skills
/api/agents
/api/workflows
```

### 25.4 V4: Prompt and Skill Studio

目标：

```text
Prompt Studio
Skill Studio
Prompt Runtime
Skill Runtime
```

### 25.5 V5: Agent Runtime

目标：

```text
Agent Studio
Agent Runtime
Agent Run History
```

### 25.6 V6: Workflow Studio

目标：

```text
Workflow Canvas
Activity Runtime
Workflow Runtime
Trigger Runtime
```

### 25.7 V7: Knowledge and Memory

目标：

```text
Knowledge Studio
Memory Studio
Qdrant
Mem0
Redis
```

### 25.8 V8: Observability and Publishing

目标：

```text
Run History
Logs
Trace
Versioning
Publishing
Export and Import
```

---

## 26. 技术栈

| 模块 | 技术 |
|---|---|
| CLI | Typer |
| Terminal UI | Rich |
| Web API | FastAPI |
| Frontend | Next.js |
| Styling | Tailwind CSS |
| Configuration | Pydantic Settings |
| Dependency Injection | dependency-injector |
| ORM | SQLAlchemy |
| Migration | Alembic |
| Local DB | SQLite |
| Vector DB | Qdrant |
| Memory | Mem0 |
| Cache / State | Redis |
| Agent Runtime | Microsoft Agent Framework |
| Observability | OpenTelemetry |
| Packaging | uv / pyproject.toml |
| Testing | pytest |
| Linting | Ruff |
| Type Checking | mypy |

---

## 27. 关键设计原则

### 27.1 Studio 与 Runtime 分离

Studio 只负责设计。

Runtime 只负责执行。

### 27.2 Resource 统一抽象

所有外部连接都通过 Resource 管理。

包括模型、数据库、向量库、记忆、API、MCP Server。

### 27.3 Prompt、Skill、Workflow 都是一等公民

Shovel 不应该只关注 Agent。

Prompt、Skill、Workflow 都应该有独立生命周期、版本、测试和发布流程。

### 27.4 Agent 是组合对象

Agent 不是单个 Prompt。

Agent 是：

```text
Model
Prompt
Skills
Knowledge
Memory
Runtime Config
Workflow
```

的组合。

### 27.5 Workflow 是核心编排能力

Workflow 是 Shovel 区别于普通 Agent 工具的关键能力。

### 27.6 本地优先，云端可扩展

第一阶段本地运行。

未来可以支持 Workspace、Remote Runtime、Team Sharing。

### 27.7 类型安全和可测试性优先

长期坚持：

```bash
ruff check
ruff format
mypy --strict
pytest
```

---

## 28. 与 Azure Data Factory 的概念映射

| Azure Data Factory | Shovel | 说明 |
|---|---|---|
| Studio | Shovel Studio | 可视化设计界面 |
| Integration Runtime | Shovel Runtime | 实际执行引擎 |
| Linked Service | Resource | 外部资源连接 |
| Dataset | Knowledge Source / Data Source | 数据或知识来源 |
| Activity | Activity | 最小执行单元 |
| Pipeline | Workflow | 工作流编排 |
| Trigger | Trigger | 触发器 |
| Monitor | Runs | 运行历史和监控 |

---

## 29. 最终愿景

Shovel 的最终目标是成为个人开发者的 AI Agent Factory。

它融合以下产品思想：

```text
OpenClaw
    +
Azure Data Factory
    +
Prompt Flow
    +
LangFlow
    +
GitHub Copilot Workspace
    +
Personal Agent Runtime
```

但 Shovel 的重点不是复制这些产品，而是形成自己的统一抽象：

```text
Resource
Prompt
Skill
Agent
Workflow
Knowledge
Memory
Trigger
Runtime
Studio
```

最终目标：

```text
让个人开发者可以通过 GUI 和 CLI 创建、测试、发布和运行自己的 AI Agent 系统。
```

---

## 30. README 中的一句话介绍

英文版本：

```text
Shovel is an AI Agent Studio and Runtime Platform inspired by Azure Data Factory.

It enables developers to visually design, test, publish, and operate Prompts, Skills, Knowledge Bases, Memories, Agents, and Workflows through a unified Studio and Runtime architecture.
```

中文版本：

```text
Shovel 是一个受 Azure Data Factory 启发的个人 AI Agent Studio 与 Runtime 平台。

它帮助开发者通过统一的可视化 Studio 和 Runtime 架构，设计、测试、发布和运行 Prompt、Skill、Knowledge Base、Memory、Agent 与 Workflow。
```

---

## 31. 总结

Shovel 的核心价值不在于单独运行一个 Agent，而在于提供一个完整的 AI Agent 开发、测试、发布和运行平台。

它的关键思想是：

```text
Studio 负责设计
Runtime 负责执行
Resource 负责连接
Prompt 负责语言模板
Skill 负责可复用能力
Agent 负责智能体组合
Workflow 负责任务编排
Knowledge 负责外部知识
Memory 负责长期上下文
Trigger 负责自动执行
Runs 负责监控和诊断
```

因此，Shovel 的最终定位应该是：

```text
Personal AI Agent Factory
```

而不是：

```text
Simple AI Agent App
```

这也是 Shovel 与普通 Agent 工具、Prompt 工具、RAG 工具和 Workflow 工具最本质的区别。