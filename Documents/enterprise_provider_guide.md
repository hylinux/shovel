# Enterprise Provider 开发指南（ES-10）

本指南描述**已实现的契约和隔离示例**，不是开启生产企业访问的操作手册。
生产 Registry/FunctionExecutor 没有注册企业 Provider，connection-backed admission
仍默认拒绝，认证 resolver/broker 默认 unavailable。个人配置不能替代组织信任或批准。

架构与阶段证据见 [security analysis](enterprise_security_analysis.md)、
[ADRs](enterprise_security_adrs.md)；攻击面与剩余风险见
[threat model](enterprise_threat_model.md)。原始需求见
[foundation](enterprise_security_foundation.md)。

## 1. 运行最小示例

从仓库根目录，在已有的 Python 3.14+、Windows x64 开发环境中运行：

```powershell
uv run --no-sync python -m scripts.demo_enterprise
```

源码为 [`scripts\demo_enterprise.py`](../scripts/demo_enterprise.py)。
依赖准备参见根 [README](../README.md)；示例不需要 `shovel init`、正在运行的 Host、
Ollama、模型或 embedding 配置、企业账号或认证 SDK。`--no-sync` 不安装或升级依赖；
尚未准备环境时先按 README 从提交的 lock 安装，而不是修改锁文件绕过错误。

脚本创建独立临时 SQLite，运行实际 migrations、Registry、InvocationService、
ExecutionEngine、FunctionExecutor、authentication lifecycle 和保护/审计内核，
关闭 Engine/数据库后删除临时目录。不读取用户 settings，不接触用户 SQLite、
Memory/Knowledge 或 Zvec，不发 HTTP 请求，不启动新 Runtime。

示例 transport 只返回固定 synthetic bytes；所有域名均为 `.example.invalid`。
凭据是硬编码的无效哨兵，不是真实 Token。`BrokerType.PYTHON_WAM` **只在内存副本中**
满足类型契约；实际执行的是 `SyntheticBroker`，没有调用 WAM、Windows 登录或 SDK。
SQLite 中 Identity 仍 disabled/unavailable，Connection 仍 disabled draft。

成功输出包含两条 `authorized_synthetic_display` 和一个统计报告：

| 场景 | 实际行为 | 报告 |
|---|---|---|
| ALLOW | READ 和 DISPLAY 分别授权、审计，再调用 whole-buffer display | `allow: succeeded` |
| DENY | READ 可用但 DISPLAY 无规则；无 display callback | `failed / data_use_denied` |
| WAITING_FOR_AUTHENTICATION | fake broker 要求交互，原子挂起同一个 Execution；timer 不唤醒 | `waiting_for_authentication / automatic_resumes: 0` |
| 手动恢复 | 脚本令 fake broker ready，显式 resume，同一 Execution 重新获取凭据 | `satisfied / same_deadline: true` |
| Derived protection | 纯内存固定 transform，继承 source、classification、rights 与 DATA lineage | `source_bound: true / body_recovery: unavailable` |
| 拒绝 embedding | 对精确 synthetic model binding 作 EMBED policy 决策，无模型客户端或向量写入 | `embedding: denied` |

预期统计：`display_frames=2`、`transport_calls=4`、`broker_calls=5`、
`audit_decisions=8`；`synthetic_only=true`、`production_ready=false`。
认证等待时可能出现只含安全错误类型的 `authentication.late_failure` 诊断，
不意味着演示失败；非预期场景失败会抛安全异常并以非零退出。
Execution result 与统计只包含 receipt/元数据；正文只从获准的 display callback 输出。
终端可重定向，因此这不是受保护查看器、no-copy 或 no-export 证明。

## 2. 架构与扩展边界

安全链路是：

```text
trusted installed FunctionContract + Capability content
  -> Registry / frozen Invocation / existing ExecutionEngine
  -> live platform permission / Risk / Policy / Approval
  -> exact EnterpriseReadPlan
  -> silent AuthenticationService / ephemeral AuthContext
  -> trusted bounded transport + protected decoder
  -> READ decision + committed audit
  -> fresh execution/source/permission/authentication checks
  -> DISPLAY decision + committed audit + whole-buffer callback
```

没有第二套 Provider Runtime、Job/WorkflowTask 或 Trigger。Workflow child 仍用同一个
Engine；Provider 不创建 Schedule、不拥有业务触发器。connection/identity/protection
服务之间用 typed values 和 ports 协作，SQL 与事务留在 repository/adapters。

| 接口/模块 | 开发者负责 | 不能做 |
|---|---|---|
| `enterprise\domain.py` / `catalog.py` | 安装可信 Manifest，绑定环境、endpoint、audience、scope 和 Function IDs | 从用户 import path、Agent 参数或远端响应动态加载实现 |
| `enterprise\execution.py` | 解析精确 read plan，复用 `EnterpriseReadHandler` | 把 connection ref 当作授权或跳过既有 Engine |
| `identity\service.py` / `ports.py` | 可信 resolver 与 silent broker adapter | Provider 自己登录、保存 Token 或自动换 CLI/device-code |
| `enterprise\transport.py` | 将一次性 credential 限制在已批准 target，调用有界 decoder | 暴露 credential/Header 给业务数据或跟随未验证 target |
| `protection\policy.py` / `service.py` | 提供可信、完整且新鲜的 PolicyState，逐用途 gate | 因为 READ 成功就授权 model、persist、export 或 index |
| `db\protection_repository.py` / `protection_audit.py` | 精确 lease/source 复查、明确审计事务、受控释放 | 在 DB write transaction 内另开同步 SQLite 写 audit |

示例的 `SyntheticConnectionPolicy` 仅在自己的 composition 中允许两个固定 Function
及精确 connection requirement，再保留普通 side-effect policy；不修改默认 evaluator。
`SyntheticPublisher` 仅批准固定 scope、第一版本和脚本已知 content hashes。
二者是**隔离测试的受信任装配**，不能复制为生产 admission 或人工确认的替代实现。
生产发布、启用、恢复等继续要求 server preview / single-use confirmation ticket；
认证完成也不等于批准这些操作。没有新增“开启企业访问”的 TOML 开关。

## 3. Provider authoring

先声明 Manifest，再安装 FunctionContract/handler，最后才创建并发布匹配的 Capability。
Contract 与 content 必须一致，包含 exact required permissions、connection requirements、
input/output schema、risk/side effects 和 retry contract；Capability 还须显式设置
`permission_boundary`。发布后版本/内容 hash 不可改写，修改应 fork Draft。

Manifest 应列出 provider/version、connection types、各 cloud/environment 的 authority、
endpoint、audience、permitted scopes、实现 IDs、protection metadata/classifications、
availability/health refs 与 minimum runtime version。Global/Mooncake 不能混用。
安装完整性、可信 catalog 和组织 trust adapter 是生产准入前提，不是用户勾选项。
示例中的 availability/health refs 只是固定 Manifest 声明，不提供真实探测服务。

每次解析 plan 都要绑定：

- workspace/user、Execution/correlation、当前 permission ceiling 和 frozen content；
- identity/profile revision、tenant/principal、connection revision、Manifest digest；
- endpoint/audience/scopes、operation、精确 resource/source version 与 generation。

使用 `EnterpriseReadPlan.validate_execution()` 和已有 target validators，
不让 LLM 拼 URL、scope、tenant、audience 或 Python 实现名。传输前后重新解析并检查
plan digest；改变 revision、source 或目的地不得复用旧授权。
传输必须只调用一次有界 decoder，返回该 decoder 构造的同一个 `ProtectedContent`。
原始入口只能是精确 source reference + DATA lineage，不能假装为派生结果或裸 JSON。
`EnterpriseReadHandler` 最大接受 262144 bytes，可收窄，不能把 streaming callback 接到
未经检查的网络输出；真实 SDK adapter 还必须在接收/缓冲阶段限制响应体。

## 4. Connection profile

`ConnectionDraft` 保存非秘密业务配置：provider/type、cloud/environment、endpoint、
tenant/resource scope，以及 identity/protection/retention 等 profile refs。
`ConnectionService` 通过安装 catalog 校验 Manifest；repository 持有 workspace/user、
revision、manifest version/digest、状态和时间。修改使用 expected revision；
未知 Provider、未启用/未验证配置和 stale revision 不能 dispatch。

禁止 Token、password、cookie、authorization header、连接字符串凭据进入 draft、
排队参数、版本快照或 SQLite。配置只保存安全 refs，不是 secret storage。
当前生产 schema 强制 Connection disabled draft/disabled；不要用 SQL、`model_copy`
或放宽 schema 绕过。示例仅对临时 process-local 副本启用，不持久化或导出该副本。
enabled/validated 是必要条件，不提供外部资源授权或数据使用权限。

## 5. Identity integration

Provider 不拥有身份会话。可信 resolver 重新验证 exact TokenRequest 与当前 profile/
Manifest/account/connection、silent mode 和**原 Execution deadline**；
AuthenticationService 执行一次有界 silent acquire，再复查绑定和新鲜度。
`AuthContext` 只供可信 transport 一次性 consume；在 `finally` 中 close，
不得返回给 Agent、写入缓存/Operation、打印 repr 或转换为通用 JSON。

交互需要、账号缺失、claims challenge 或 broker 不可用要转为 typed authentication
wait，不能忙轮询或自动登录。`EnterpriseReadHandler` 已示范此映射：
同一个 durable Execution 原子挂起，request/deadline/operation digest 固定；
手动 resume 后仍需 fresh acquire。`satisfied` 是追加 completion fact 的投影，
不是可复用的 authenticated boolean。timer、旧 completion、普通聊天或 Approval
都不能替代新认证；cancel/timeout 后迟到 credential 必须关闭且不能 dispatch。

示例 DERIVE handler 只演示 broker 已 ready 的固定路径，**不提供完整的交互等待映射**；
需要交互的读取入口复用 `EnterpriseReadHandler`，不能直接把 DERIVE handler 扩成通用
网络/模型 adapter。真实 Windows WAM SDK、broker-required/non-fallback、取消和组织策略
仍需单独获准的 Windows opt-in 验证；离线 fake broker 不证明这些属性。

## 6. Data protection 与派生数据

每个企业正文必须由 `ProtectedContent` 携带完整 metadata：
source/identity/principal/generation、ProtectionContext、classification、rights、
policy source/version、retention/revalidation/expiry、DATA/CONTROL lineage、
authorization snapshot refs 与 content digest。未知 label/rights/policy 或 protection
缺失应拒绝；不能降级为 personal content，不能把 `lifecycle_state=active` 当作授权。

平台 permission、外部 identity/resource authorization、内容 rights、数据用途 policy、
Risk 和 Approval 是正交条件。每次 READ、DISPLAY、TRANSFORM、SEND_TO_MODEL、
EMBED、INDEX、PERSIST、EXPORT、SHARE、Memory/Knowledge 使用都要独立 gate。
决策须绑定 exact content/context digest、source、identity/connection revisions、
action、destination、policy version 与时限；model egress 还必须绑定精确 ModelProfile、
provider/model/endpoint/environment。localhost 模型、SQLite 和 Zvec 也不是豁免目的地。

`ProtectionService.derive()` 对所有 DATA/CONTROL 依赖做保守 merge，不推测组织 label
的大小关系：rights 取交集、限制合并、有效期取最短，保留所有来源和 source-bound
失效要求。派生结果是当前 ProtectionService 所属 process-instance 的 transient handle，
不是可持久恢复正文。另一个 ProtectionService（包括 repository 内部 gate）不能消费
该 handle；不能访问私有 gate、伪造 SourceContentRef 或重跑模型假装恢复。

示例在明确 audit/session UoW 内执行固定、无外部副作用的 transform，并在返回 receipt
之前提交 audit。它只保留进程内派生 metadata；不显示、不存储派生正文，不调用模型，
随后演示独立 EMBED 决策为 DENY。不能把这段纯计算改成未纳管的模型、文件、网络输出。
真正派生输出的 display/persist/export adapter 必须另做完整接线与验收。

`SQLiteProtectionRepository.use_bound()` 当前支持 READ/DISPLAY 的整 buffer 释放：
先提交 audit，再在新检查中验证 lease、祖先、权限、source fences，才调用 consumer。
release marker 让不确定 display 不自动重放。企业 metadata repository 是 metadata-only；
持久 generated body 的协议、恢复及加密尚未接受。SourceReference 可重新查询也必须
重新验证身份/来源/policy，不能复用旧明文。Zvec 只是隔离的 rebuildable 候选索引，
SQLite/source fences 仍为权威；无自动企业 embedding/index writer。

## 7. Secure logging

日志只记录允许的状态、opaque IDs、错误类型、计数、时间和安全 diagnostic refs。
禁止正文、派生文本、Token/Header/cookie、claims challenge、账户信息、原始 SDK
异常链/response、source 路径和未经审核的 label 名称进入 log/trace/telemetry。
`SecretStr`、安全 repr 和 regex 都不能替代正确边界：不要 dump model、原异常或整个请求。
开发模式、console、stderr、SQL echo、browser 转发和日志导出同样适用。

Policy audit 不是普通 debug log：使用 safe schema，绑定确切 session/scope/Execution，
提交失败就不能宣称授权已完成。audit 保存经设计允许的绑定元数据，不包含正文或
凭据；这些元数据仍需访问控制和组织 retention 审批。不要为“排查认证”打开 raw SDK
wire logging，也不要以 synthetic 的终端展示证明真实企业内容可以输出到终端。

### Host 的 H1 legacy containment

Chat 与 Execution 已建立企业数据处理范围；范围内创建/释放 ProtectedContent 后，
裸字符串、工具结果与派生 dict 也不能进入旧 AG-UI、模型、MCP、Memory/Knowledge、
embedding/cache/index 或个人正文存储链路。子 task/线程共享标记，不能靠嵌套 scope
清除。该标记只用于保守拒绝，不是 source classification 或组织授权。

自定义 Provider 不得先 unwrap/stringify 再调用普通 client；READ/DISPLAY 不授予其他
action/destination。受保护的正向出口必须另有可信、逐使用授权、保留上下文的 adapter；
没有这些能力时显式拒绝，不能通过 ALLOW 设置或换本地模型绕过。Artifact/clipboard
目前无可信应用 adapter。synthetic derive handler 不向普通 Execution output 写正文或
派生 payload；示例报告只由外层控制器投影固定状态。细节见分析第 24 节。

## 8. Testing 与验收

示例专用回归：

```powershell
uv run --no-sync pytest backend\tests\test_enterprise_sample.py --no-cov
uv run --no-sync ruff check scripts\demo_enterprise.py backend\tests\test_enterprise_sample.py
uv run --no-sync ruff format --check scripts\demo_enterprise.py backend\tests\test_enterprise_sample.py
uv run --no-sync mypy scripts\demo_enterprise.py
```

sample tests 运行真实 Engine/SQLite，检查两次授权 display、拒绝无输出、同 Execution
认证恢复/原 deadline、派生继承与拒绝 embedding，扫描所有 SQLite 表的正文/凭据哨兵，
检查 disabled 持久配置、临时目录清理、display 异常不泄漏、精确 publish hash、
默认 production deny，以及独立 subprocess 的公开启动命令。
测试禁止 httpx HTTP 并设置无效用户数据库配置，确保示例不依赖用户运行配置。

集成回归与 Provider 新增失败用例参照 `test_enterprise_execution.py`、
`test_enterprise_failures.py`、`test_enterprise_authentication.py`、
`test_enterprise_persistence.py`、`test_enterprise_vector.py` 和
`test_protection_policy.py`。至少验证错 tenant/audience/cloud、revision 变更、
unknown protection、source 删除/撤权、policy 变更、审计失败、并发恢复、超时/取消/
迟到结果、重复 wakeup、所有真实 sink 零调用、SQLite/diagnostics 哨兵无泄漏。
不要只断言 mock ALLOW；检验实际 callback/store/network 边界。

离线 tests 不能接真实企业、读取用户配置或打开 live model/WAM 开关。新的真实 Provider
应另有明确 opt-in、批准的 tenant/account/测试数据、实际 SDK/出口与恢复证明。
H1 全出口、H2 external-write reconciliation、H3 组织 trust/source lifecycle、
H4 retention/physical storage 仍是上线门槛，详见威胁模型。
