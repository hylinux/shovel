你正在为 Shovel 实现 Enterprise Security Foundation。

在开始修改代码前，请完整分析当前仓库的架构、目录、领域模型、数据库模型、Dependency Injection、Configuration、Capability Registry、ExecutionEngine、Memory、Knowledge、SQLite、Zvec、Microsoft Agent Framework 集成和现有测试。

不要假设某个组件不存在，也不要在已有实现之外创建重复架构。

============================================================
一、Shovel 产品定位
============================================================

Shovel 不是通用跨平台 Agent Framework。

Shovel 是：

- Windows-only
- Local-first
- 面向个人用户的 Personal Agent
- 深度集成 Windows
- 深度集成 Microsoft / Enterprise 环境
- 支持 Main Agent、Skills、Workflow/Pipeline 和 Proactive Services
- 使用统一 Capability Registry
- 使用唯一 ExecutionEngine
- 使用 SQLite 作为结构化数据 Source of Truth
- 使用 Zvec 作为可重建的本地语义索引
- 使用 Microsoft Agent Framework 作为 Agent 基础设施之一

明确不支持：

- macOS
- Linux
- Android
- iOS
- 通用跨平台兼容层
- 为未来可能的跨平台支持预留没有实际用途的抽象

不要创建：

- IPlatformAdapter
- MacOS adapter
- Linux adapter
- PlatformFactory
- 跨平台 Notification/FileSystem/Auth 抽象
- 第二套 ExecutionEngine
- 第二套 Capability Registry
- 第二套 Workflow Runtime
- 第二套 Event Runtime

可以直接深度使用 Windows 能力，包括：

- Microsoft Entra ID
- MSAL
- Windows Web Account Manager, WAM
- Windows Credential Manager
- Windows DPAPI
- Windows Notifications
- Windows Job Objects
- PowerShell
- Windows Event Log
- Windows Task Scheduler
- Windows Security APIs

但任何具体 Windows API 或第三方库的选择，都必须先检查当前项目技术栈和依赖兼容性。

============================================================
二、当前 Capability 架构
============================================================

Shovel Capability 分为四类：

1. Built-in Function

   面向 Windows 本地操作系统的原子能力，例如：

   - filesystem
   - process
   - application
   - clipboard
   - notification
   - system
   - shell
   - Windows integration

2. Utility Function

   无副作用或低副作用的确定性功能，例如：

   - text
   - data
   - datetime
   - archive
   - hash
   - encoding
   - schema
   - template

3. Enterprise Provider

   公司或企业业务系统能力，例如：

   - Kusto
   - Azure
   - DFM
   - Azure DevOps
   - IcM
   - Microsoft 365
   - GitHub
   - 未来的内部支持系统

4. Shovel Platform Function

   Shovel 平台自身的领域能力，例如：

   - memory
   - knowledge
   - skill
   - workflow
   - automation
   - prompt
   - capability management

四类能力必须统一注册到现有 Capability Registry。

Enterprise Provider 不是第二套 Agent Runtime，也不是第二套 Capability Runtime。

Provider 只负责：

- 声明 Capability
- 验证 Provider 特定参数
- 调用外部企业系统
- 将外部结果转换为 Shovel 标准类型
- 返回带 ProtectionContext 的结果

Provider 不负责：

- 自己实现认证框架
- 自己维护 Token Cache
- 自己保存用户密码
- 自己实现 Trigger
- 直接启动 Workflow
- 绕过 ExecutionEngine
- 绕过 Permission、Risk、Approval 或 Data Protection Policy
- 直接操作 Shovel 内部 SQLite 表
- 把 Access Token 暴露给 Agent

============================================================
三、实施目标
============================================================

本任务的目标是设计并实现以下三个相互协作、但职责正交的机制：

1. Identity Plane
2. Enterprise Connection Profile
3. Data Protection Plane

它们共同构成 Shovel Enterprise Security Foundation。

总体调用链为：

User / Trigger
    -> Main Agent / Skill / Workflow
    -> Capability Registry
    -> Capability Permission and Risk Check
    -> Enterprise Connection Resolver
    -> Identity Plane
    -> Enterprise Provider
    -> ProtectedContent
    -> Data Protection Plane
    -> Policy Decision
    -> ExecutionEngine
    -> Display / Model / Memory / Knowledge / SQLite / Zvec / Artifact

必须保持以下关注点相互独立：

- Authentication
- External resource authorization
- Shovel capability permission
- Data protection
- Risk evaluation
- User approval
- Execution orchestration

任何一个机制都不能取代其他机制。

============================================================
四、统一的五问安全模型
============================================================

任何 Enterprise Capability 调用都必须能够回答：

1. WHO

   由 Identity Plane 回答：

   - 当前以哪个企业身份访问？
   - Account、Tenant 和 Claims 是什么？
   - 是否需要重新登录？
   - 是否能以 silent 方式完成认证？

2. WHERE

   由 Enterprise Connection Profile 回答：

   - 连接哪个 Provider？
   - 连接哪个环境、Cloud、Tenant 和 Endpoint？
   - 资源范围是什么？
   - 使用哪个 Identity Profile？
   - 使用哪个 Data Protection Profile？

3. MAY

   由外部系统 Authorization 和 Shovel Permission 共同回答：

   - 用户是否有权访问目标资源？
   - 当前 Agent、Skill 或 Workflow 是否可以调用该 Capability？

4. WHAT MAY HAPPEN TO THE DATA

   由 Data Protection Plane 回答：

   - 数据能否显示？
   - 能否处理、总结或提取？
   - 能否发送给指定模型？
   - 能否生成 Embedding？
   - 能否写入 SQLite、Zvec、Memory、Knowledge 或 Artifact Store？
   - 能否导出或发送到其他系统？

5. DO IT NOW

   由 Risk 和 Approval 回答：

   - 当前操作是否需要用户批准？
   - 已有 Approval 的 Scope 是否覆盖本次具体调用？

不要把这五个问题压缩为单个布尔值。

============================================================
五、Identity Plane
============================================================

Identity Plane 只解决：

- 用户是谁
- 使用哪个企业账户
- Tenant 是什么
- 如何登录
- 如何获得短时访问上下文
- 如何处理 Token 生命周期
- 如何处理 Claims Challenge
- 如何识别需要用户重新认证
- 如何提供安全的认证诊断

Identity Plane 不负责：

- 数据分类
- Sensitivity Label
- DLP
- 是否允许写入 Zvec
- Agent Permission
- User Approval
- Workflow 编排
- Provider 业务逻辑

------------------------------------------------------------
5.1 Windows 认证主路径
------------------------------------------------------------

Shovel 是 Windows-only 桌面应用。

企业身份认证的默认主路径为：

- Microsoft Entra ID
- Delegated Authentication
- MSAL
- Windows Web Account Manager, WAM
- Silent-first
- 必要时 Interactive Authentication

Azure CLI 或 Device Code 只能作为显式启用的开发或诊断模式，不得作为默认产品认证路径。

不要使用以下方案：

- 用户名和密码认证
- 在 Shovel UI 中采集公司密码
- 保存用户名和密码
- 抓取浏览器 Cookie
- 从其他应用复制 Token
- 把 Access Token 放入 Function arguments
- 把 Access Token 返回给 Agent
- 把 Token 写入 SQLite、日志、Trace、Workflow state 或 Artifact

如果当前 Python 技术栈不能直接可靠地完成 MSAL + WAM 集成，请设计清晰的 Windows Native Authentication Broker 边界。

该 Broker 可以是独立的本地受信任组件，但必须：

- 只负责 authentication
- 通过最小化的本地 IPC contract 暴露能力
- 对调用者身份进行验证
- 不返回 Refresh Token
- 不允许任意客户端请求任意 Scope
- 不将 Token 写入磁盘业务存储
- 不创建第二套通用 Runtime
- 不承担 Provider 业务逻辑

在选择进程内实现还是独立 Broker 进程前，请先调查当前仓库技术栈并编写 ADR。

------------------------------------------------------------
5.2 Identity Profile
------------------------------------------------------------

Identity Profile 代表一个逻辑企业身份，而不是某个具体系统连接。

建议领域模型：

IdentityProfile
- id
- display_name
- identity_type
- account_type
- tenant_binding
- authentication_mode
- broker_type
- account_selector
- enabled
- status
- created_at
- updated_at

示例语义：

- Microsoft Work Identity
- Customer Tenant Guest Identity
- Lab Identity

Identity Profile 不得保存：

- Password
- Access Token
- Refresh Token
- Client Secret
- Raw Cookie

------------------------------------------------------------
5.3 Token Request
------------------------------------------------------------

Provider 不能自行构造任意 Token 请求。

定义受控的 TokenRequest：

- connection_profile_id
- provider_id
- tenant_id
- authority
- resource or audience
- allowed scopes
- claims_challenge
- correlation_id
- interaction_mode
- force_refresh only when policy allows

Scope、Audience、Authority 和 Tenant 必须来自：

- Enterprise Connection Profile
- Provider Manifest
- 经审核的配置

不得来自：

- LLM 自由生成
- Agent 参数
- Prompt 文本
- 外部检索内容
- 未验证的 Provider 返回值

------------------------------------------------------------
5.4 Auth Context
------------------------------------------------------------

Provider 不获得可长期保存的 Credential 对象。

Identity Plane 向受信任的 AuthenticatedTransport 提供短生命周期 AuthContext。

AuthContext：

- 只在当前调用生命周期内有效
- 不可序列化到 Workflow State
- 不可写入 SQLite
- 不可写入 Artifact
- 不可传给 Agent
- 不可出现在 repr、日志或异常中
- 不可成为标准 FunctionResult 的一部分

建议使用专门的 Secret/Redacted 类型，防止意外打印。

------------------------------------------------------------
5.5 认证状态
------------------------------------------------------------

至少支持：

- READY_SILENT
- INTERACTION_REQUIRED
- REAUTHENTICATION_REQUIRED
- CLAIMS_CHALLENGE_REQUIRED
- AUTHORIZATION_REQUIRED
- DEVICE_OR_POLICY_BLOCKED
- ACCOUNT_UNAVAILABLE
- CONNECTION_MISCONFIGURED
- UNAVAILABLE

对于 Proactive Service：

silent authentication 失败时，不允许整个 Workflow 崩溃或无限重试。

Execution 应进入：

WAITING_FOR_AUTHENTICATION

然后：

- 创建 Deferred Interaction
- 通知用户
- 等待用户完成认证
- 通过现有 Wakeup/Resume 机制恢复 Execution
- 支持跳过本次运行
- 支持暂停主动服务
- 支持取消 Execution

不能通过长时间保持内存中的 coroutine 等待用户。

------------------------------------------------------------
5.6 Auth Error Taxonomy
------------------------------------------------------------

至少定义：

- AuthenticationRequiredError
- ReauthenticationRequiredError
- ClaimsChallengeError
- WrongTenantError
- TokenAudienceMismatchError
- AccountNotFoundError
- BrokerUnavailableError
- ConditionalAccessBlockedError
- DeviceComplianceRequiredError
- ConsentRequiredError
- EntitlementRequiredError
- JitActivationRequiredError
- ExternalAuthorizationDeniedError
- AuthConfigurationError

错误对象需包含：

- stable error code
- safe user-facing message
- retryable
- user_action_required
- correlation_id
- diagnostic_ref

不得包含 Token、Secret、Authorization Header 或敏感 Claims。

============================================================
六、Enterprise Connection Profile
============================================================

Enterprise Connection Profile 回答：

“Shovel 使用什么身份，以什么方式，连接哪个企业系统和资源范围？”

它不是 Credential，也不是 Policy Engine。

建议领域模型：

EnterpriseConnectionProfile
- id
- display_name
- provider_id
- environment
- cloud
- endpoint
- tenant_binding
- resource_scope
- identity_profile_ref
- protection_profile_ref
- provider_settings
- enabled
- status
- created_at
- updated_at
- revision

必须支持：

- Microsoft Azure Global
- Azure China / Mooncake
- 不同 Tenant
- 不同 Subscription
- 不同 Kusto Cluster 和 Database
- Microsoft 365
- DFM
- ADO
- IcM
- 未来企业 Provider

不要假设：

- 所有 Microsoft 服务使用相同 Authority
- Global Azure 和 Azure China 使用相同 Endpoint
- 一个全局 current_account 足够
- 一个 Tenant 可以覆盖所有 Connection

------------------------------------------------------------
6.1 Connection Profile 安全边界
------------------------------------------------------------

Connection Profile 可以保存：

- Provider ID
- Endpoint
- Environment
- Cloud
- Tenant ID
- Subscription ID
- Database
- Resource Scope
- Identity Profile reference
- Protection Profile reference
- Non-secret Provider configuration

Connection Profile 不得保存：

- Password
- Access Token
- Refresh Token
- Client Secret
- API Key plaintext
- Cookie
- Authorization Header

如果未来需要 API Key 或 Certificate，应只保存：

- credential_ref
- certificate_ref

Secret 本体由 Windows Credential Manager、DPAPI 或批准的 Secret Store 管理。

------------------------------------------------------------
6.2 Provider Manifest
------------------------------------------------------------

每个 Enterprise Provider 必须提供 Manifest，声明：

- provider_id
- provider_version
- supported connection types
- supported clouds/environments
- permitted authorities
- permitted audiences
- permitted scopes
- authentication modes
- capability definitions
- data classifications emitted by the Provider
- supported protection metadata
- availability probe
- health check
- minimum runtime version

Provider 不得在运行时请求 Manifest 未声明的 Scope 或 Audience。

------------------------------------------------------------
6.3 Enterprise Resource Reference
------------------------------------------------------------

定义统一的 EnterpriseResourceRef：

- provider_id
- connection_profile_id
- resource_type
- resource_id
- parent_resource_id
- tenant_id
- source_version
- etag or revision where available

例如：

- Microsoft 365 Email
- SharePoint Document
- Teams Message
- Kusto Database
- Kusto Query Result
- Azure Subscription
- Azure Resource
- ADO Work Item
- IcM Incident

Audit、Authorization、Protection、Lifecycle 和 Derived Data 都使用相同的 Resource Reference。

============================================================
七、Data Protection Plane
============================================================

Data Protection Plane 回答：

“企业数据被读取后，可以怎样使用？”

它必须是跨 Provider 的 Runtime 横切机制，不能只存在于 Microsoft 365 Provider 中。

它适用于：

- Email
- Calendar content
- Teams messages
- SharePoint and OneDrive files
- Kusto query results
- Azure diagnostics
- DFM content
- ADO work items and wiki
- IcM incidents
- Enterprise artifacts
- Enterprise-derived Memory
- Enterprise Knowledge
- Embeddings

Data Protection Plane 不负责：

- 登录
- Token acquisition
- Workflow orchestration
- Agent reasoning
- Capability discovery

------------------------------------------------------------
7.1 ProtectedContent
------------------------------------------------------------

Enterprise Provider 不得直接返回裸的：

- str
- dict
- bytes
- arbitrary model object

Provider 必须返回：

ProtectedContent[T]

建议包含：

- value or secure content handle
- source_ref
- protection_context
- provenance
- authorization_snapshot_ref
- lifecycle_metadata
- content_digest
- created_at

避免在 repr 或日志中输出 value。

对于大型内容，优先使用 secure content handle 或 ArtifactRef，不要让正文在多个对象间复制。

------------------------------------------------------------
7.2 ProtectionContext
------------------------------------------------------------

ProtectionContext 至少表达：

Source:
- provider_id
- connection_profile_id
- tenant_id
- enterprise_resource_ref

Classification:
- sensitivity_label_id
- sensitivity_label_name where policy permits
- classification_id
- classification_rank or policy reference

Protection:
- encrypted
- protected
- rights metadata
- policy source
- policy version

Rights:
- read
- display
- copy
- extract
- transform
- summarize
- embed
- persist
- export
- share
- send_to_model

DLP:
- applicable
- decision reference
- restrictions

Retention and lifecycle:
- retention_policy_ref
- expires_at
- source_bound
- delete_with_source
- revalidate_at

Provenance:
- original or derived
- source references
- derivation operation
- model reference where applicable

不要硬编码公司的 Label 名称或顺序。

实际 Label ID、优先级和保护行为必须来自组织配置或受信任 Policy Provider。

------------------------------------------------------------
7.3 Data Actions
------------------------------------------------------------

统一定义：

- READ
- DISPLAY
- PROCESS
- SUMMARIZE
- EXTRACT
- TRANSFORM
- SEND_TO_MODEL
- EMBED
- CACHE
- INDEX
- PERSIST
- WRITE_TO_MEMORY
- WRITE_TO_KNOWLEDGE
- EXPORT
- SHARE
- COPY_TO_CLIPBOARD
- WRITE_TO_FILE
- SEND_TO_PROVIDER

每次涉及企业数据的跨边界行为，都必须评估对应 DataAction。

------------------------------------------------------------
7.4 Data Destinations
------------------------------------------------------------

定义明确的 DataDestination：

- USER_SCREEN
- WINDOWS_CLIPBOARD
- LOCAL_SQLITE
- LOCAL_ZVEC
- LOCAL_ARTIFACT_STORE
- LOCAL_FILE_SYSTEM
- ENTERPRISE_MODEL
- EXTERNAL_MODEL
- ENTERPRISE_PROVIDER
- NETWORK_ENDPOINT
- MEMORY_STORE
- KNOWLEDGE_STORE
- WORKFLOW_STATE
- APPLICATION_LOG
- AUDIT_STORE

Policy Decision 必须至少考虑：

- ProtectionContext
- DataAction
- DataDestination
- Identity context
- Connection context
- Execution context
- Model profile
- Agent/Skill/Workflow identity
- Source lifecycle
- Current policy version

------------------------------------------------------------
7.5 Policy Decision
------------------------------------------------------------

定义结构化 PolicyDecision：

- ALLOW
- DENY
- REQUIRE_PROTECTION
- REQUIRE_REAUTHENTICATION
- REQUIRE_SOURCE_REVALIDATION
- REQUIRE_USER_ACTION

不要把 REQUIRE_USER_ACTION 直接等同于 Approval。

PolicyDecision 必须包含：

- decision_id
- rule references
- reason codes
- user-safe explanation
- required protections
- policy version
- evaluated_at
- revalidate_at
- audit metadata

禁止 Skill、Agent 或 Provider 自己覆盖 DENY 决策。

------------------------------------------------------------
7.6 Derived Data
------------------------------------------------------------

以下全部属于 Derived Data：

- extracted text
- normalized content
- chunk
- embedding
- summary
- classification
- named entity
- structured record
- generated response
- personal or enterprise memory
- knowledge record
- model cache
- query cache

Derived Data 默认继承 Source ProtectionContext。

禁止隐式降低敏感级别。

多个来源合并时：

- 使用可配置 Protection Merge Policy
- 选择组织策略要求的有效保护
- 保存所有 source references
- 不允许 Agent 自己选择较低保护
- 不要假设 Label 名称固定
- 不要仅依赖字符串比较 Label

定义：

ProtectionContext.merge(...)

但具体合并逻辑必须委托给 Policy Engine。

------------------------------------------------------------
7.7 Model Egress
------------------------------------------------------------

任何发送到模型的企业数据都属于 DataAction.SEND_TO_MODEL。

定义 ModelProfile：

- model_profile_id
- provider
- endpoint/environment
- enterprise_approved
- permitted classifications
- permitted actions
- permitted tenant scope
- retention characteristics
- data boundary metadata
- embedding_allowed
- generation_allowed
- enabled

Data Protection Plane 必须在模型调用之前评估：

ProtectedContent
+ SEND_TO_MODEL
+ ModelProfile
+ ExecutionContext

未经 Policy 允许，不得将企业数据发送到任意外部模型或用户自定义 Endpoint。

模型返回的结果必须成为 Derived ProtectedContent，而不是普通字符串。

------------------------------------------------------------
7.8 SQLite、Zvec、Memory 和 Knowledge
------------------------------------------------------------

SQLite 仍然是结构化数据 Source of Truth。

Zvec 仍然是可重建的语义索引。

但是：

- Zvec 不是安全边界
- Local-first 不代表数据可随意持久化
- BitLocker 或本机存储不能替代 Data Protection Policy
- Enterprise content 不能因为进入本地数据库而丢失 ProtectionContext

对于 Enterprise Content，默认采用：

Reference-first
Copy-minimized
Source-bound
Policy-controlled

Zvec record 至少保存：

- vector
- source_ref
- protection_context_ref
- tenant_id
- connection_profile_id
- identity scope reference
- data classification reference
- lifecycle state
- indexed_at
- source version
- policy version

除非 Policy 明确允许，不要默认在 Zvec 中保存完整邮件正文、附件全文或完整 Chunk。

SQLite 保存：

- source metadata
- resource reference
- protection context
- derivation graph
- lifecycle state
- policy decision references
- authorization snapshot metadata

不要在 SQLite 保存：

- Access Token
- Refresh Token
- Authorization Header
- plaintext enterprise credential

------------------------------------------------------------
7.9 Enterprise Search
------------------------------------------------------------

Enterprise semantic search 必须是：

- identity-aware
- connection-aware
- tenant-aware
- protection-aware
- authorization-aware
- lifecycle-aware

搜索流程：

Query
    -> determine identity and allowed connection scope
    -> Zvec candidate retrieval
    -> protection filter
    -> authorization freshness evaluation
    -> source validity evaluation
    -> optional source reauthorization
    -> optional source refetch
    -> ProtectedContent results
    -> Data Protection evaluation
    -> Agent

不得仅因为向量仍在 Zvec 中就向 Agent 暴露内容。

------------------------------------------------------------
7.10 Authorization Snapshot
------------------------------------------------------------

为了避免每条向量结果都产生一次远程调用，定义 AuthorizationSnapshot：

- principal reference
- tenant
- resource scope
- decision
- captured_at
- source version
- policy version
- freshness state
- revalidate_at

Snapshot 不是永久授权。

Policy Engine 决定：

- 可以使用 Snapshot
- 必须重新验证 Source
- 必须拒绝访问
- 必须等待认证

不要硬编码统一 TTL。

------------------------------------------------------------
7.11 Enterprise Memory
------------------------------------------------------------

Personal Memory 与 Enterprise-derived Memory 必须隔离。

Enterprise Memory 默认：

- source-bound
- protection-aware
- lifecycle-aware
- tenant-aware
- connection-aware
- revocable
- not automatically permanent

Enterprise Memory 至少保存：

- source_refs
- protection_context_ref
- tenant_id
- connection_profile_id
- derivation metadata
- lifecycle state
- revalidation state

当源数据：

- 删除
- 失效
- 权限撤销
- Label 改变
- Retention 到期
- 内容版本变化

对应 Memory 必须：

- invalidate
- relabel
- re-evaluate
- recompute
- or delete

具体动作由 Policy 决定。

============================================================
八、数据生命周期和 Derivation Graph
============================================================

实现 Derived Data lineage。

建议模型：

DerivedDataRecord
- id
- output_type
- source_refs
- protection_context_ref
- operation_type
- executor identity
- capability id and version
- model profile where applicable
- created_at
- source versions
- policy decision ref
- lifecycle state

必须支持事件：

- source_changed
- source_deleted
- source_access_revoked
- source_label_changed
- source_retention_changed
- connection_disabled
- identity_disabled
- policy_changed
- model_profile_disabled

这些事件不得由 Provider 直接启动 Workflow。

正确路径：

Provider or lifecycle observer
    -> normalized durable event
    -> existing Event infrastructure
    -> subscription matching
    -> existing ExecutionEngine
    -> revalidation/invalidation execution

Capability 永远不拥有 Trigger。

============================================================
九、Capability Runtime 集成
============================================================

Identity Plane、Connection Profile 和 Data Protection Plane 必须接入现有 Capability 调用链。

建议逻辑顺序：

1. Resolve capability
2. Freeze capability/provider versions
3. Resolve enterprise connection
4. Check Shovel capability permission
5. Evaluate base risk
6. Request authentication context
7. Call Provider through AuthenticatedTransport
8. Wrap result as ProtectedContent
9. Resolve protection metadata
10. Evaluate requested DataAction and destination
11. Apply required protection
12. Evaluate Approval when applicable
13. Continue the existing Execution lifecycle
14. Store only policy-approved data
15. Emit redacted audit and diagnostic records

不要创建 EnterpriseExecutionEngine。

============================================================
十、AuthenticatedTransport
============================================================

Provider 不能手工拼接和记录 Authorization Header。

实现统一 AuthenticatedTransport。

职责：

- 接收经过验证的 ProviderRequest
- 根据 Connection Profile 请求 AuthContext
- 注入短时认证信息
- 执行请求
- 处理 Claims Challenge
- 处理 correlation ID
- 清理短时 Secret
- 对 Header、Query、Body 和 Error 做安全脱敏
- 返回 ProviderResponse

AuthenticatedTransport 不负责：

- Provider data mapping
- Protection classification
- Workflow orchestration
- Agent reasoning

需要防止：

- Header 泄漏
- Token 出现在 exception
- 重定向到非受信任 Host
- Connection endpoint 与 Token audience 不匹配
- Provider 请求 Manifest 外的 Host
- LLM 控制完整 URL
- SSRF
- 任意 Scope 请求

============================================================
十一、日志、审计和诊断
============================================================

严格区分：

1. Application Log
2. Security Audit
3. Execution Event
4. Diagnostic Record
5. Telemetry

不得默认记录：

- Email body
- Teams message body
- File content
- Prompt containing enterprise data
- Model response containing protected data
- Access Token
- Refresh Token
- Authorization Header
- Cookie
- Sensitive Claims
- Full Function arguments
- Full Provider response
- Embedding input

允许记录的典型元数据：

- capability id and version
- provider id and version
- connection profile id
- identity profile id
- resource reference hash
- action
- destination
- policy decision id
- protection label reference
- result status
- duration
- size
- error code
- correlation id
- execution id

所有诊断对象必须提供 safe serialization。

============================================================
十二、数据库和迁移要求
============================================================

先检查当前 SQLite/SQLAlchemy/Repository 模型。

新增数据必须通过：

- Domain model
- Repository
- Migration
- Application service

不得直接让 Agent 或 Provider 执行内部数据库 SQL。

可能需要的存储模型包括：

- identity_profiles
- enterprise_connection_profiles
- provider_manifests
- protection_contexts
- protection_profiles
- policy_decisions
- enterprise_resource_refs
- authorization_snapshots
- derived_data_records
- lifecycle_records
- model_profiles

不要机械创建以上所有表。

先判断：

- 当前项目是否已有等价模型
- 哪些对象应持久化
- 哪些对象应仅存在于 Runtime
- 哪些信息适合值对象或 JSON column
- 哪些 Secret 明确不能持久化

所有 Migration 必须可回滚或提供明确的安全迁移策略。

============================================================
十三、必须遵守的架构不变量
============================================================

ENT-SEC-01

Shovel 是 Windows-only。不得增加 macOS/Linux 抽象或实现。

ENT-SEC-02

Enterprise Provider 不拥有认证实现，只能通过 Identity Plane 获取短时认证上下文。

ENT-SEC-03

Access Token、Refresh Token、Password、Cookie 和 Authorization Header 永远不得进入 Agent、LLM、Workflow State、Memory、Knowledge、SQLite、Zvec、Artifact、Event 或普通日志。

ENT-SEC-04

Enterprise Connection Profile 不保存 Secret，只保存安全引用。

ENT-SEC-05

所有 Enterprise Content 进入 Shovel Runtime 后必须携带 ProtectionContext。

ENT-SEC-06

Derived Data 默认继承 Source ProtectionContext，不允许隐式保护降级。

ENT-SEC-07

Read Authorization 不代表 Process、Summarize、Embed、Persist、Export 或 Model Egress Authorization。

ENT-SEC-08

任何 SEND_TO_MODEL、EMBED、INDEX、PERSIST、EXPORT、SHARE、WRITE_TO_MEMORY 和 WRITE_TO_KNOWLEDGE 操作必须经过 Data Protection Policy。

ENT-SEC-09

Enterprise Semantic Search 必须是 Identity-aware、Authorization-aware、Protection-aware 和 Lifecycle-aware。

ENT-SEC-10

Enterprise Memory 和 Knowledge 默认 Source-bound。

ENT-SEC-11

企业内容不能因为进入 SQLite、Zvec、Memory、Artifact 或 Workflow 而丢失 ProtectionContext。

ENT-SEC-12

Identity、Connection、Authorization、Data Protection、Permission、Risk 和 Approval 必须保持正交。

ENT-SEC-13

Scope、Audience、Authority、Tenant 和 Endpoint 不得由 LLM 自由生成。

ENT-SEC-14

未经 Policy 明确允许的 Enterprise Content，不得默认进入长期 Memory、Knowledge 或 Vector Store。

ENT-SEC-15

Provider、Identity Plane 和 Data Protection Plane 必须复用唯一 Capability Registry 和唯一 ExecutionEngine。

ENT-SEC-16

Capability 和 Provider 永远不拥有 Trigger。

ENT-SEC-17

任何未知或无法确定的数据保护状态使用 deny-by-default。

ENT-SEC-18

Local-first 不等于 Policy bypass。本地存储仍必须经过 Data Protection Policy。

============================================================
十四、实施方式
============================================================

不要一次性实现全部内容。

按以下阶段执行。

每个阶段必须：

- 先检查现有代码
- 输出拟修改文件
- 说明复用的现有组件
- 说明可能冲突
- 实现最小完整垂直切片
- 编写单元测试
- 编写集成测试
- 运行 formatter、linter、type checker 和 tests
- 修复本阶段引入的问题
- 提供阶段总结
- 不修改无关模块

------------------------------------------------------------
Phase ES-0：Repository Analysis
------------------------------------------------------------

只分析，暂不修改生产代码。

检查：

- package/module structure
- Domain layer
- Application layer
- Infrastructure layer
- Dependency Injection
- Configuration
- Capability Registry
- ExecutionEngine
- Event system
- Interaction system
- Permission
- Risk
- Approval
- Audit
- SQLite/SQLAlchemy
- Zvec
- Memory
- Knowledge
- Artifact Store
- Microsoft Agent Framework
- test architecture

输出：

1. Current architecture map
2. Existing reusable components
3. Gaps
4. Conflicts with this specification
5. Recommended module placement
6. Migration risks
7. Proposed implementation phases
8. Unresolved questions that block secure implementation

不要仅返回泛泛建议。引用具体文件、类和模块。

------------------------------------------------------------
Phase ES-1：Architecture Decision Records
------------------------------------------------------------

创建或更新 ADR，至少覆盖：

1. Enterprise Identity Plane
2. Windows MSAL/WAM Broker strategy
3. Enterprise Connection Profile
4. Data Protection Plane
5. ProtectedContent and ProtectionContext
6. Enterprise data persistence and vector indexing
7. Derived data lifecycle
8. AuthenticatedTransport
9. Enterprise search reauthorization
10. Logging and secret redaction

ADR 必须记录：

- context
- decision
- alternatives
- consequences
- security assumptions
- unresolved dependencies
- rollback/migration impact

对不能从当前仓库或正式依赖确认的内容标记为 unresolved，不要猜测。

------------------------------------------------------------
Phase ES-2：Core Contracts
------------------------------------------------------------

实现或复用：

- IdentityProfile
- EnterpriseConnectionProfile
- EnterpriseResourceRef
- TokenRequest
- AuthContext
- AuthenticationState
- Auth error taxonomy
- ProtectedContent[T]
- ProtectionContext
- DataAction
- DataDestination
- PolicyDecision
- AuthorizationSnapshot
- DerivedDataRecord
- ModelProfile

要求：

- strong typing
- immutable value objects where appropriate
- explicit serialization rules
- secret-safe repr
- schema validation
- no circular dependency
- versioned contracts
- complete unit tests

------------------------------------------------------------
Phase ES-3：Enterprise Connection Registry
------------------------------------------------------------

实现：

- create profile
- update profile
- disable profile
- list profiles
- resolve profile
- validate provider compatibility
- validate tenant/cloud/endpoint consistency
- optimistic concurrency
- migration
- repositories
- application services

此阶段不得保存 Token 或 Secret。

------------------------------------------------------------
Phase ES-4：Identity Plane Kernel
------------------------------------------------------------

实现：

- Identity Profile registry
- EnterpriseAuthService contract
- TokenRequest validation
- AuthenticatedTransport contract
- authentication states
- auth diagnostics
- claims challenge model
- silent-first orchestration
- deferred authentication integration
- redaction

暂时可以使用 deterministic fake broker 完成测试，但 fake broker 只能存在于 tests/dev fixtures，不得成为生产默认实现。

------------------------------------------------------------
Phase ES-5：Windows Authentication Broker
------------------------------------------------------------

在已经批准的 ADR 基础上实现：

- Microsoft Entra delegated authentication
- MSAL
- WAM
- account selection
- tenant binding
- silent authentication
- interactive fallback
- claims challenge
- cancellation
- timeout
- correlation ID
- logout/account disconnect semantics

如果需要本地 Native Broker：

- 定义最小 IPC contract
- 验证调用者
- 限制 Scope/Audience
- 限制允许的 Provider Manifest
- 防止任意客户端使用
- 安全清理 Token
- 测试异常和进程退出

不要在此阶段接入 Kusto、M365 或其他具体业务 Provider。

------------------------------------------------------------
Phase ES-6：Data Protection Kernel
------------------------------------------------------------

实现：

- ProtectionContext
- ProtectedContent
- DataAction
- DataDestination
- PolicyDecision
- Policy evaluation interfaces
- Protection propagation
- Protection merge
- derived content lineage
- deny-by-default fallback
- policy audit
- secure serialization

必须证明：

- ProtectionContext 不会在 transform 中丢失
- Derived Content 自动保留 lineage
- 未知保护状态不能被持久化
- DENY 不能被 Provider/Skill/Agent 覆盖

------------------------------------------------------------
Phase ES-7：Persistence and Zvec Integration
------------------------------------------------------------

实现最小安全路径：

- SQLite protection metadata
- enterprise resource references
- derived data metadata
- lifecycle state
- Zvec metadata schema
- source-bound indexing
- protection-aware search filtering
- invalidation primitives

不要默认保存完整 Enterprise Content。

优先测试：

- source reference only
- embedding metadata
- content digest
- protection reference
- authorization snapshot
- deletion/invalidation

------------------------------------------------------------
Phase ES-8：ExecutionEngine Integration
------------------------------------------------------------

把三套机制接入现有调用链。

实现最小垂直切片：

Fake Enterprise Provider
    -> Connection Resolver
    -> Fake Identity Broker
    -> AuthenticatedTransport
    -> ProtectedContent
    -> Data Protection evaluation
    -> safe display or deny
    -> existing ExecutionEngine

然后扩展：

- WAITING_FOR_AUTHENTICATION
- deferred interaction
- resume
- cancellation
- policy denial
- source invalidation
- redacted audit

不得创建第二套 Runtime。

------------------------------------------------------------
Phase ES-9：Security and Failure Tests
------------------------------------------------------------

至少覆盖：

Identity:
- wrong tenant
- wrong audience
- missing account
- claims challenge
- silent auth failure
- broker unavailable
- token redaction
- cancellation
- timeout

Connection:
- Azure Global/Mooncake mismatch
- disabled connection
- endpoint mismatch
- unsupported Provider
- stale revision
- invalid scope

Data protection:
- unknown label
- protection metadata missing
- read allowed but embed denied
- model egress denied
- persist denied
- multiple source merge
- derived data inheritance
- source deleted
- permission revoked
- policy changed
- stale authorization snapshot

Leak prevention:
- Token not present in logs
- Token not present in exceptions
- Token not present in SQLite
- protected content not present in telemetry
- protected content not present in repr
- enterprise content not written to Zvec when denied

Execution:
- WAITING_FOR_AUTHENTICATION survives restart
- authentication resume works
- user cancellation works
- policy denial is terminal unless policy changes
- duplicate wakeup is idempotent

------------------------------------------------------------
Phase ES-10：Documentation and Sample Provider
------------------------------------------------------------

提供：

- architecture documentation
- provider authoring guide
- connection profile guide
- identity integration guide
- data protection guide
- secure logging guide
- testing guide
- threat model
- minimal sample enterprise provider

Sample Provider 必须：

- 不连接真实企业系统
- 不使用真实 Token
- 使用 synthetic data
- 展示完整安全路径
- 展示 ALLOW 和 DENY
- 展示 WAITING_FOR_AUTHENTICATION
- 展示 Derived Data protection propagation

============================================================
十五、当前明确不实施的内容
============================================================

本任务暂不实现：

- 完整 Kusto Provider
- 完整 Microsoft 365 Provider
- 邮件同步
- 邮件全文向量化
- DFM Provider
- Azure Provider
- ADO Provider
- IcM Provider
- 真实公司 Sensitivity Label 排序
- 真实公司 DLP 规则
- 自定义的组织合规策略结论
- macOS/Linux 支持
- Cloud Worker
- Service Principal secret flow
- Managed Identity for desktop
- 任意 External Model endpoint
- 任意 Scope token acquisition

上述功能必须等待 Identity、Connection 和 Data Protection Foundation 完成后再实现。

============================================================
十六、安全设计原则
============================================================

使用以下默认原则：

- deny by default
- least privilege
- minimal data copy
- reference first
- source bound
- explicit identity
- explicit tenant
- explicit destination
- short-lived authentication context
- no token exposure
- protection metadata propagation
- query-time authorization awareness
- auditable decisions
- restart-safe execution
- secret-safe diagnostics
- immutable published contracts
- versioned provider manifests

不要为了“先跑起来”而绕过这些原则。

============================================================
十七、输出要求
============================================================

首先只执行 Phase ES-0 和 Phase ES-1。

暂时不要开始 Phase ES-2 之后的生产代码实现。

在本轮输出中提供：

1. Repository architecture analysis
2. File-level gap analysis
3. Conflicts with existing implementation
4. Proposed module structure
5. ADR documents
6. Data-flow diagram using Mermaid
7. Threat model
8. Trust boundaries
9. Proposed database changes
10. Proposed IPC boundary if a Windows Native Auth Broker is needed
11. Phased implementation plan
12. Test strategy
13. Unresolved security/compliance questions
14. Exact files planned for Phase ES-2
15. Stop and wait for review

如果仓库现有设计与本 Prompt 冲突：

- 不得静默覆盖现有设计
- 不得创建平行架构绕过冲突
- 必须指出具体冲突
- 必须提出最小迁移方案
- 必须说明兼容性影响

如果发现本 Prompt 中的假设无法由当前代码、正式 SDK 或已安装依赖支持：

- 明确标记为 unresolved
- 不要编造 API
- 不要创建假的生产实现
- 可以使用测试替身验证 Contract
- 停止在需要架构决策的位置

开始执行 Phase ES-0。