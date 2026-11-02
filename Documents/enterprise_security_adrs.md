# Enterprise Security Foundation：ES-1 ADR 提案

**架构 ADR 仍为 Proposed / Awaiting Review；用户已单独批准方案补充及 ES-2 Core
Contracts、ES-3 禁用配置管理及 ES-4 身份/认证/持久交互内核实施。该范围批准不是组织 policy/WAM/存储架构
或后续上线批准。**

**ES-5 另行批准范围：隔离 Python WAM 离线验收 spike，不是生产 Broker 路线
批准；未登录、未获取 Token。**

依据 [ES-0 Repository Analysis](enterprise_security_analysis.md) 和需求文件
`Documents\enterprise_security_foundation.md`。这里的“决策”均为拟议决策，
不覆盖既有 [Platform ADRs](platform_adrs.md)，运行时提案不代表已经实施。
证据基线、官方 SDK 链接、file-level gaps、信任边界、测试和 ES-2 文件清单在配套分析。

## ENT-ADR-01 — Enterprise Identity Plane

**Context**

`domain\identity.py` 只是 ID generator；`Resource.identity_ref` 和 CredentialStore
处理 keyring/env secret refs，不提供 Entra identity、tenant/account 或 silent acquisition。
现有 Permission/Risk/Approval 是 capability gate，不是外部登录/授权。
`PlatformSettings.user_id=owner` 与 Native Memory 的 local-user namespace 也不是企业账户。

**Proposed decision**

在 `identity` 放置逻辑 IdentityProfile、AuthenticationState、受控 TokenRequest、
短时 AuthContext、auth errors 和 authentication-only service/broker contract。
原用户/workspace Scope 保持不变，增加明确 enterprise principal/tenant/profile绑定，
禁止 display name/username 当唯一授权身份。仅 trusted transport 消费 AuthContext；
Provider 不获得 CredentialStore、SDK client、Token getter 或长期 credential 对象。

silent-first。后台遇到需要交互时产生 durable authentication request，并经既有
Engine suspend/Interaction/resume恢复；human登录不产生 capability permission、DLP
ALLOW 或 Approval。错误完整覆盖需求 taxonomy，safe serialization 不含 raw claims/
SDK descriptions。claims challenge 仅可信 target响应产生，不能来自 Agent 参数。

**Alternatives**

复用 keyring/env API 作为认证框架会混淆长期secret与delegated context；Provider独立
MSAL/cache会重复认证并绕过scope控制；一个global current_account无法绑定多tenant。
均不采用。Azure CLI/device code仅未来显式开发诊断，不是生产默认。

**Consequences**

需要逻辑身份注册、account映射和安全状态机；旧 personal ownership 不重写。
认证问题成为可解释待办，而非generic executor failure或无限retry。

**Security assumptions**

微软SDK/Windows auth组件、批准的profile配置和装配代码可信；账号成功不证明资源授权。
Python AuthContext减少泄漏但不是同进程恶意代码隔离或可证明内存零化。

**Unresolved dependencies**

分析 B01/B03/B04/B05/B10：稳定principal公开API、租户/app/consent、UI会话、WAM
模式保证与取消、caller身份、等待期限及disconnect语义。

**Rollback / migration impact**

ES-2只有contracts；后续identity表只存安全metadata。回滚禁用enterprise admission，
不将Token搬到SQLite/TOML、不改旧Memory用户ID；auth waiting facts和旧receipts保留。

**ES-4 已实施切片**

scope/revision-bound 身份 metadata、silent-only authentication service、安全 status/errors
和持久 authentication request 已接入既有 Runtime/Platform/HTTP/CLI。生产身份保持
disabled/unavailable，resolver/broker 明确 unavailable。request 仅为已挂起 Execution
同事务创建 fact/projection/notification；用户选择原 Execution deadline，不扩大等待
期限或预算。没有认证完成入口、自动 Engine 挂起/resume guard/wakeup；这些仍属于 ES-8。
cancel/dismiss 不授予权限，不隐式取消或恢复 Execution。详见分析第 16 节。

## ENT-ADR-02 — Windows MSAL/WAM Broker Strategy

**Context**

仓库为Python 3.14 x64、Generic Host、FastAPI、MAF、静态浏览器UI；有pywin32，
没有MSAL/.NET项目。本机有.NET SDK，但这不证明native产品组件可用。
官方PyPI的pymsalruntime 0.20.7有cp314 win_amd64 wheel，不能因ABI假设强制Native Broker。
MSAL公开API同时说明可能静默non-broker fallback，interactive阻塞与HWND约束必须验证。

**Proposed decision**

优先对进程内MSAL Python/WAM做**接受性验证**，不是立即选择生产实现。
门槛包括：锁定依赖兼容、公开API证明并限制WAM-required、exact account/tenant、
可信parent HWND、人触发interactive、silent-only proactive、timeout/cancel/late-result
丢弃、safe SDK logging、Global/China/guest/CA/claims矩阵以及应用disconnect。
任一所需场景不能证明安全就保持UNAVAILABLE，不自动browser/device-code fallback。

若验证表明不能可靠满足，再修订/批准authentication-only Windows native component
路线；私有最小IPC设计采用分析第8节边界。No Refresh Token、no arbitrary scope/
URL、no Provider业务、no general transport/Workflow/runtime。短时AT如需跨私有IPC，
只能进入trusted Identity adapter/AuthenticatedTransport生命周期，不能进入普通响应。

**Alternatives**

无调查直接.NET broker会增加签名、安装、IPC攻击面；直接Azure CLI/设备码不符合
默认产品路径；用私有SDK字段判broker状态不可维护；把HTTP业务搬到broker破坏职责。
跨平台MSAL参数/adapter不纳入Shovel。

**Consequences**

可能最少新增Python认证依赖，也可能经另审新增Windows-native打包项目。
browser workbench没有天然可用HWND；Windows Service/session 0不能靠future abstraction
解决。`asyncio.to_thread`外层取消不是SDK UI取消证明。

**Security assumptions**

interactive Windows user session、正式SDK与受信任安装；broker SDK fallback必须可
阻断/可验证。SID/PID/loopback/签名python.exe不能独自认证Shovel业务代码。

**Unresolved dependencies**

B01–B05、B10、B12；国别云、UI/取消、模式可观测性、实际IPC API/互认证/签名/launcher。
这里没有发明一个已可用的Native API，也未验证MSAL.NET对全部问题的解决能力。

**Rollback / migration impact**

ES-0/1不安装SDK，ES-2不建native项目。生产路线失效时disable/unavailable并保存
无secret pending request；清理应用状态不删除Windows/Office账户。未来binary/签名/
installer回退须独立方案，不能无声fallback到弱认证。

**ES-5 接受性 spike 证据**

单独 PEP 723 脚本/lock 在 CPython 3.14 x64 Windows 安装 MSAL 1.39.0、
pymsalruntime 0.20.7 并完成公开参数和 synthetic core binding 检查；主项目 SDK、
配置、lock 和 Runtime 不改变。不构造 SDK client 或调用账户/认证方法。
离线结果通过但 production_ready=false；wheel/API 存在不是 native WAM 初始化证明。
官方 API 仍明示 broker 初始化失败时的 non-broker fallback，尚无已验证的
发送前 WAM-required/真实取消保证，不据此启用生产或立即选择 Native 实现。
详细命令、测试、授权范围和未验证门槛见分析第 17 节。

## ENT-ADR-03 — Enterprise Connection Profile

**Context**

`ConnectionRequirement(name, connection_ref)`已存在但不解析企业连接，
`security.services.PolicyEvaluator`明确deny connection-backed execution。
Resource connector配置不是identity+protection+cloud的企业连接配置。

**Proposed decision**

`enterprise`中定义并管理ConnectionProfile，明确provider/version、environment/cloud、
endpoint/tenant/resource scope、identity/protection refs、non-secret schema、enabled/status、
timestamps与revision。ProviderManifest是trusted installed FunctionContract/handler catalog
的附属binding，不是第二套Capability Registry；发布/admission冻结manifest版本/hash。
capability kind仍为Function，四类产品Function作为目录类别。

authority/audience/scopes/hosts只从approved profile+manifest+policy解析，并彼此绑定；
不从LLM完整URL、prompt或unchecked response解析授权范围。Profile CRUD通过application
service/repository及optimistic concurrency；Agent不能以profile更新自授信任。
未验证profile/manifest、不同cloud不一致或disabled都不允许invoke。

**Alternatives**

单全局account/tenant会错误混用；把secret写provider_settings或复用credential identity_ref
语义不合规；数据库驱动任意import会绕过trusted Function registration。

**Consequences**

创建可disabled Draft；identity/protection kernel未就绪时不伪造健康/READY。
live profile变化可撤销旧executions，但不能扩大冻结权限、替换其endpoint或silent换tenant。

**Security assumptions**

manifest/configuration来自批准的信任根；企业Provider是trusted business mapping。
JSON credential key guard不是non-secret配置的完整验证，必须有明确schema/allowlist。

**Unresolved dependencies**

B01/B02/B06/B12：cloud/provider实际组合、发布配置审核、profile refs生命周期、runtime
version权威、网络/代理/资源范围规则。

**Rollback / migration impact**

ES-3新增profile persistence及迁移，保持旧ConnectionRequirement与发布hash不变。
不在旧content内注入default manifest字段导致rehash；新binding有独立contract version。
未解析旧ref继续deny，回滚保留disabled metadata，不写secret或放宽既有gate。

**ES-3 implemented slice**

migration 15及现有 Runtime/Platform/HTTP/CLI接线只提供disabled Draft管理。
manifest来自可信安装/DI metadata catalog，默认目录为空；未知provider不能通过请求
注入manifest。保存冻结version/hash；manifest变更需重新保存reviewed revision，
删除后仍可查看/禁用旧配置。revision写事务、scope隔离和DB guards均在SQLite执行。
configuration validation不代表identity/protection/transport已就绪，始终executable=false。
没有enable、真实Provider、Token请求或network probe；组织信任根和完整setting语义规则
仍是后续门槛，详见配套分析第15节。

## ENT-ADR-04 — Cross-Provider Data Protection Plane

**Context**

现有security policy管理capability side effects/Approval，不区分企业数据的READ、
SEND_TO_MODEL、EMBED、PERSIST等。Main Agent、Knowledge、Memory和模型middleware
处理裸文本。本机SQLite/Zvec或localhost model不能被当作合规例外。

**Proposed decision**

独立`protection`领域，以ProtectionContext+DataAction+DataDestination+identity/connection/
execution+ModelProfile+live source/policy版本作结构化decision。
六种结果ALLOW/DENY/REQUIRE_PROTECTION/REAUTHENTICATION/SOURCE_REVALIDATION/
USER_ACTION保留不同语义；unknown/missing/unavailable deny。
没有组织rule、classification authority或required protection实现时不放行。

每个出口执行gate，包括model tool rounds、compaction、summary、extractor、embedding、
query cache、keyword/FTS、Workflow Condition/state、display、clipboard/files、SQLite、
Zvec、Memory、Knowledge、Artifact和audit。现有Permission/Risk/Approval独立保留；
Approval不能覆盖DENY，REQUIRE_USER_ACTION也不自动创建Approval。

ES-2增加DataUseBinding及非默认可序列化PolicyPermit；绑定content/context digest、
source/identity/connection generations/revisions、execution/actor/action/destination/model。
permit在签发与消费时委托Runtime-owned PolicyAuthority验证签发、当前policy和撤销，
不是caller传入ALLOW就能release。typed obligations须全部具有匹配kind/target/binding/
version/time的可信evidence；unknown obligation/verifier无默认fulfilled。
生产authority/verifier只允许可信DI注入；同进程伪造Python对象不是此类型系统能隔离的攻击。

**H1硬门槛：**streaming正文释放前检查；完整输出DLP要求buffer时不能先转发token
再检查final response。ES-6必须覆盖当前AG-UI逐event转发、buffer retention及所有模型出口。

**Alternatives**

仅M365做DLP漏掉其他Provider；仅在provider返回时检查漏掉派生/缓存/模型重调用；
把read grant或local-first当allow违反数据动作独立性；固定Label名称/rank不适用组织策略。

**Consequences**

真实Provider启用前要完成裸出口containment；未知企业ingress不能默认personal。
trusted policy实现是配置/规则解释，不加载用户Python/eval。失败状态显式输出safe code。

**Security assumptions**

policy/Label/rights来源被组织认可，application gate不能被Provider/Skill/Agent覆盖。
classification检测不是凭启发式prompt或用户一个enterprise_approved布尔值完成。

**Unresolved dependencies**

B06–B09/B11/B12：组织policy权威、MIP/DLP与受保护查看器、模型data boundary、
ingress/历史数据、代理/retention/审计和revocation freshness。

**Rollback / migration impact**

不替换security.services.PolicyEvaluator。enterprise出口关闭而不是回到无policy处理。
personal兼容行为不借机重写；新legacy bypass guard不允许因降级binary消失。

## ENT-ADR-05 — ProtectedContent, ProtectionContext and Derived Data

**Context**

Engine/Workflow输入输出与检查点为JSON；Skill/Agent返回text；现有frozen Value可默认dump。
Memory、Chunk/Parent、ChatMessage和模型结果没有企业保护传播契约。
dataclass `repr=False`或SecretStr无法阻止普通serialization复制正文。

**Proposed decision**

Enterprise Provider仅产生ProtectedContent[T]；source/ref、protection、provenance、
authorization snapshot、lifecycle、digest/version/time是完整envelope的一部分。
正文使用非默认序列化容器/短时handle；日志/repr不含value。普通JSON/state只接受明确
PERSIST许可的版本化safe reference，ref不是read capability，使用时仍验证gate。

保护在transform/chunk/vector/summary/answer/Memory/entity/cache等派生中继承；
ProtectionContext.merge委托policy，保存全部来源和versions，跨tenant默认不合并，
不允许implicit label downgrade。模型输出包装derived envelope后再评价release/store。
AuthContext与ProtectedContent是不同容器，Token永远不成为content value/result。

DATA和CONTROL lineage同等传播；Condition、branch/filter/route影响输出即纳入source
union，同源不同role不折叠为一条边。merge结果不得丢来源/derived metadata。
正文支持canonical JSON/bytes，构造和释放时核对digest；context另有独立digest绑定。
metadata可dump不代表可log；正文容器无默认JSON/pickle/copy，不用repr生成内容hash。

**Alternatives**

给任意dict附一个可忽略label字段会在mapping/json flatten时丢失；把正文直接放operation
output或state意味着未经许可持久化；用固定最大label/字符串比较并非组织merge policy。

**Consequences**

需要explicit materialization与safe ref adapters；existing raw JSON consumers不能自动
读取企业value。transient handle重启后不可恢复；只有准确版本的source ref可重授权/refetch，
generated summary等无source copy的内容需要批准的Artifact，否则显式不可恢复。
大型内容减少复制，但不宣称Python任意内存/SDK副本都可擦除。

**Security assumptions**

所有纳管envelope出口通过trusted adapters；Provider代码可信但return type仍强验证。
用户手动pasted data和unmanaged MCP分类必须另解决，不能靠类型系统自动推断来源。

**Unresolved dependencies**

B06/B08/B09/B11：真实metadata格式、rights实现、digest敏感性、secure handle/Artifact
生命周期、source重新获取和历史ingress。

**Rollback / migration impact**

新contract version独立于旧CapabilityContent hash/fingerprint，不给旧published payload
补default字段。旧API不能反序列化protected ref时明确unsupported/deny，不能降成text。

## ENT-ADR-06 — Enterprise Persistence and Vector Indexing

**Context**

SQLite是Document/Chunk/Memory正文及lifecycle权威。Knowledge Zvec包含text FTS副本；
Memory向量已有scope/revision但无enterprise binding。DeletionOutbox、MemoryIndexOutbox、
flush-before-ack与rebuild generations已经存在。BlobRef不是运行中的secure CAS。

**Proposed decision**

默认reference-first、source-bound、无企业正文持久化。按需增加queryable protection/source/
snapshot/decision metadata与已有业务实体sidecars，不创建重复Document/Memory runtime。
Zvec企业collection/generation复用现有store/sink基础设施，默认无text字段；记录ref、
identity/tenant/connection/classification/lifecycle/source/policy versions和indexed_at。

EMBED、SEND_TO_MODEL、INDEX、PERSIST及WRITE_TO_MEMORY/KNOWLEDGE独立通过policy；
metadata/vector/cache也是受保护派生数据。no-embed不能自动转FTS落盘或伪造空Chunk成功。
enterprise writes保留原子SQLite事实/outbox；Zvec flush成功才ack，cleanup失败仍query deny。
personal与enterprise query/projection隔离，SQLite fallback也gate。

**Alternatives**

直接旧Knowledge全文schema复制邮件不符合copy-minimized；“SQLite加密/BitLocker足够”
不解决目的地policy；第二套企业DB/Memory/vector backend重复已有source-of-truth与重建能力。

**Consequences**

需要typed repositories、policy-aware read/write与新index generations。
source不可获取且无approved body copy时不能rebuild，状态明确unavailable，不claim完成。
不把历史无标签内容自动重索引为企业或personal。

ES-2引用明确区分transient/source-resolvable/persisted artifact。durable workflow
checkpoint不得依赖纯transient输出；ArtifactRef形状不证明已存在、加密或获准落盘。
不重放已完成model child制造“恢复成功”，refetch必须匹配source version并重新授权。

**H4硬门槛：**后续为正文、metadata、vector、summary、control lineage、artifact、
cache/audit分别取得retention/offline/revocation/delete矩阵；WAL/backup及不可用行为
也在范围内，不因为metadata-only或local-first默认无限保留。

**Security assumptions**

所有direct SQL/CLI/search/fallback路径纳管；at-rest protections已兑现才persist。
向量可能泄漏内容，Zvec不是安全边界，本轮未证明其原生加密/安全删除。

**Unresolved dependencies**

B07–B09/B11/B13：retention、DB/WAL/FTS/backup/caches、encryption keys、删除与可重建性、
历史隔离和old binary访问。

**Rollback / migration impact**

ordered additive steps与CHECK/trigger安全迁移；consistent backup/repeatability/hash ID
测试。保持旧personal集合；回滚disable enterprise并保留tombstones，old binary拒绝
无policy读取；不因schema additive就声称downgrade安全。

## ENT-ADR-07 — Derived Lifecycle and Durable Events

**Context**

Document revision/lifecycle、DeletionOutbox、MemoryEpoch/index outbox能处理本地失效，
但Entity/DocumentDerived不是跨输出lineage。InProcessEventBus明确非耐久，
Proactive global pause会跳过自动业务admission，不能成为安全revocation开关。

**Proposed decision**

DerivedDataRecord记录全部source versions、context、operation/executor/capability/model、
decision及lifecycle；source/connection/identity/policy/model变更形成耐久metadata事件。
同事务先invalidate/relabel/revalidation-pending并bump generation，立即阻断旧数据访问/
egress/写入。outbox再交给既有bus/matching，幂等admission进入唯一ExecutionEngine
完成后续revalidation/recompute/delete；Provider仅emit，不拥有Trigger或launch Workflow。

采用at-least-once+scoped stable event/admission keys；已完成child不重复。
业务pause/quiet/cooldown不丢security invalidation；metadata失效先完成，cleanup可以重试。
late callbacks与rebuild在commit/release前重查generation。

**Alternatives**

仅内存event可能重启丢revocation；Provider直接启动Workflow绕过统一admission；
复用DeletionOutbox表为任意生命周期消息混淆语义。复用机制而非塞不同领域到同一表。

**Consequences**

新增durable fact/outbox/delivery/admission metadata，不新增Event Runtime。
跨store不承诺Exactly Once；policy决定不同source变化应invalidate/relabel/recompute/delete。

**Security assumptions**

可信observer提供source事实和单调版本；local generation检查参与事务。
远程撤权和已发model request不是可撤回的跨系统事务。

**Unresolved dependencies**

B08/B09/B12：事件来源、远程freshness、source versions、audit/retention与recompute幂等。
security维护不受业务pause影响的具体订阅/权限装配需ES-8验证。

**Rollback / migration impact**

保留既有MemoryEpoch和personal deletion语义；新source/profile generations不可回退
导致resurrect。outbox未处理时保持deny，不丢记录或恢复旧index可见性。

## ENT-ADR-08 — AuthenticatedTransport and Effect Safety

**Context**

httpx已安装；PublicWebTransport有scope/DNS-pinning prior art，但禁止private IP，
不能直接覆盖企业网络。现有embedder错误会拼response.text；它不是enterprise
secret-safe transport。Engine dispatch fact先于executor内部真正HTTP发送。

**Proposed decision**

trusted Provider使用受控ProviderRequest/Response，只有operation、validated relative path/
resource parameters、批准的headers/body。transport独立验证connection/manifest/source
bindings后向Identity Plane获取短时context，注入认证、保持correlation，HTTPS/TLS验证，
默认no redirects、无Agent完整URL/任意headers/proxy/scope覆盖。
external challenge仅从已批准target可信响应按受控格式提取，不能改变authority/audience。
结果body/header/error safe容器不自动dump；错误转taxonomy，不保留敏感exception chain。

transport不做classification或Provider mapping；Provider不读取token、组装Auth header。
批准business side effect必须在发送前。non-idempotent发送后结果不明进入reconciliation，
不因refresh/claims/timeout自动重试。HTTP replay只在trusted operation证明安全时允许、
次数有界；现有Engine中记录not_sent/sent/received/uncertain metadata，不另建Engine。

**Alternatives**

Provider自行httpx/MSAL会造成scope/header泄漏；通用任意URL client有SSRF；
全禁private IP阻断企业系统；“401一定无effect”或“POST一定可retry”都非可信证明。

**Consequences**

需要网络/代理/TLS与operation scope规则、HTTP阶段receipt、挑战解析和late result清理。
auth wait在已证not-sent阶段才可安全恢复；不能拿Engine早期dispatch flag猜实际effect。

**H2硬门槛：**先在既有Execution/attempt事实提交durable dispatch intent，再向网络发送；
SQLite提交和HTTP发送不可能一个原子事务。崩溃恢复只有可信not-sent/replay-safe
证据才能重试；“intent已写但未见response”不能推断未发出，默认uncertain并reconcile。
非幂等外部effects无法因token refresh、401或timeout盲重放，不另建dispatch Runtime。

**Security assumptions**

注册business handler/manifest可信，operation参数不能越出resource scope；Provider响应
仍不允许扩展授权。TLS trust/proxy必须经过批准，而非trust-env任意重写。

**Unresolved dependencies**

B01/B04/B08/B12：audience验证公开SDK证据（access token可能opaque，不自行解JWT充当
授权验证）、private network/proxy、trusted challenge与effect replay/reconciliation合同。

**Rollback / migration impact**

没有SDK/网络implementation的阶段返回unavailable，不fallback到legacy client。
新HTTP receipt保留于既有Execution事实；回退停止发新请求，不盲重放uncertain effects。

**ES-4 已实施切片**

仅增加 exact-bound `ProviderRequest` 和 `AuthenticatedTransport` Protocol：
无任意 URL/header、无 SDK/HTTP implementation、无真实 Provider dispatch。
silent context 在 acquisition 后及 resolver 重查后验证绑定/有效期；未交付和晚到结果关闭。
这不是 native cancellation、durable dispatch intent 或 uncertain effect 安全恢复的证明。

## ENT-ADR-09 — Enterprise Search Reauthorization

**Context**

RetrievalService已把Zvec视候选并回查SQLite resource/document/vector state/hash，
但scope主要是resource可读状态，未使用企业principal/tenant/freshness。
搜索返回plain passage并记录query；keyword/parent expansion和Memory SQLite fallback
也可能释放正文。ORM AgentScope说明不等于实际执行。

**Proposed decision**

先解析explicit identity、allowed connection/tenant/source scope，policy允许query
processing/embedding/cache/index actions后获取候选，候选过滤和回查SQLite source/
protection/authorization snapshot/lifecycle。policy决定snapshot可用、reauth/revalidate/
refetch或deny；无统一TTL，无“Zvec命中=可读”。materialize前再次gate，
返回ProtectedContent或安全refs，并在Agent模型出站前单独SEND_TO_MODEL gate。

vector/keyword/parent expansion/document read/Memory fallback/CLI全部一致；
policy stale/unavailable不回退裸text。query/returned refs的log/cache也经过对应目的地
检查。排序/reranking不能先看未授权正文，metrics不包含正文。

**Alternatives**

仅top_k后SQL resource过滤不满足身份/保护；每条候选必远程授权可能成本大，
因此允许policy控制snapshot而非永久缓存；将read auth复用于模型发送不接受。

**Consequences**

复用现有RRF、candidate retrieval、revision checks，加上policy/source gate。
可能减少可返回结果或明确等待认证；不能用未授权命中填满页面/模型上下文。

**Security assumptions**

snapshot来源/版本/principal可信，freshness由批准policy决定；source生命周期不会因
vector记录仍在而恢复权限。cross-tenant scope不能由Agent参数自选扩大。

**Unresolved dependencies**

B06–B09/B11/B13：源端授权接口、版本与refetch、acceptable TTL/freshness、
query分类、旧CLI和历史数据路径。

**Rollback / migration impact**

保留personal检索行为但enterprise route保持deny；新sidecar/index generation隔离，
旧API/旧binary不理解context时拒读，不能去掉context再转plain passage。

## ENT-ADR-10 — Logging, Security Audit and Secret Redaction

**Context**

现有development trace以counts/timing为主，HTTP日志已避免真实path/query；
但trace函数没有field allowlist，browser console原样转发，Runtime.redact仅替换已配置
model/embedder keys，SDK/HTTP/validation errors可能包含新Token/正文/claims。
Execution/Approval/Memory revision facts不是统一Security Audit。

**Proposed decision**

区分application log、security audit、execution event、diagnostic和telemetry。
enterprise对象只提供allowlisted safe serialization：capability/provider versions、
profile/decision refs、受控resource digest、action/destination、status/error code、
correlation/execution、size/duration/time。Label名称/principal可识别信息仅政策允许时显示，
不以默认diagnostic方式记录。

禁止正文、prompt/model output、full args/provider response、embedding input、
tokens/cookies/Auth headers/raw claims；SQL echo/SDK PII/raw exception chain不能成为旁路。
safe errors在SDK adapter处生成，regex仅defense-in-depth，不按“截断到500字”脱敏。
审计和metadata持久化也受policy；required audit写入失败明确阻止相应动作，不假成功。
synthetic sentinels贯穿error/log/DB/WAL/vector/state/model transport验证。

**Alternatives**

把现有Runtime.redact当general DLP不覆盖动态Token；默认log full request然后mask会
先发生copy/泄漏；复用RetrievalLog保存企业query不符合metadata-only审计。

**Consequences**

需要集中safe schema与audit repository/service，但不增加Telemetry Runtime。
支持安全correlation/diagnostic ref，而不是把敏感SDK附加信息转给普通UI或Agent。
console/Windows通知只发送safe status；terminal重定向也属于数据目的地问题。

**H3硬门槛：**组织policy来自可信、完整性/版本/撤销受验证的安装/catalog/DI，
个人TOML/AppSetting只可收窄；不允许用户布尔值或Agent参数替换组织approval、
authority、obligation verifier和model分类边界。具体信任根与验证实现仍需组织决策。

**Security assumptions**

所有enterprise adapter/SDK logger使用经过测试的配置和serialization；开发模式没有
policy豁免。trusted code不主动绕过；不声称任意同进程插件无法访问body。

**Unresolved dependencies**

B05/B06/B09/B11：audit retention/tamper evidence/可用性、SQL/WAL/backup权限、
资源hash敏感性、日志导出、Windows Event Log是否组织要求以及mandatory audit范围。

**Rollback / migration impact**

保持现有personal诊断；enterprise safe schema版本化。回退关闭enterprise处理，
不恢复raw logging、不自动删除已获准audit。未来日志/backup清理按policy和安全迁移执行。

## ENT-SEC 需求对照

| 不变量 | 覆盖 ADR | 必須证明的边界 |
|---|---|---|
| 01 Windows-only | 02,03 | 无新增跨OS adapter；Windows生产/CI迁移明确 |
| 02 Provider不拥有auth | 01,02,08 | 仅trusted transport使用短时AuthContext |
| 03 Token/Password/Cookie/Header不进业务数据 | 01,02,05,08,10 | 无默认序列化/错误链/log/state/store泄漏 |
| 04 Connection只保存安全refs | 01,03 | typed non-secret schema、安全profile persistence |
| 05 企业content有ProtectionContext | 04,05 | 入口强制envelope；未纳管ingress不能假personal |
| 06 derived继承，不降级 | 04,05,07 | policy-owned merge、全source lineage |
| 07 read不授予后续actions | 04,06,09 | distinct action/destination/model gates |
| 08 model/embed/index/persist/export/share/Memory/Knowledge均gate | 04,06,08,09 | 所有actual egress/store，而非只Provider回调 |
| 09 identity/auth/protection/lifecycle-aware search | 07,09 | query-time freshness/source回查与materialization gate |
| 10 Memory/Knowledge默认source-bound | 06,07,09 | revoked/expired sources使派生结果失效 |
| 11 任何store/workflow保留context | 05,06,07 | safe ref绑定和读取重查，no naked JSON fallback |
| 12 机制正交 | 01,03,04,08 | auth/外部授权/permission/policy/risk/approval不混为boolean |
| 13 LLM不自由生成token scope/target | 01,03,08 | approved Manifest/Profile binding与transport validation |
| 14 企业长期store默认deny | 04,06 | unknown/no explicit grant无持久化/embedding |
| 15 唯一Registry/Engine | 03,07,08,09 | trusted Function安装、既有admission/lease/child/recovery |
| 16 Capability/Provider不拥有Trigger | 03,07 | lifecycle observer -> durable event -> existing matching/Engine |
| 17 unknown protection deny | 04,05,09 | unmet obligations/unavailable policy不能降级ALLOW |
| 18 local-first不绕policy | 04,06,10 | SQLite/Zvec/cache/Artifact/audit/本机model也受gate |

**当前边界：**ES-2契约、ES-3配置管理及ES-4身份/认证/持久交互内核已实施；
migration 15/16只在隔离测试验证，生产Runtime的resolver/broker仍unavailable。
没有真实企业认证、SDK安装、自动认证挂起/完成/恢复或enterprise deny解除。
H1–H4仍是后续阶段硬门槛，
不能把契约测试当作其运行时验证或组织批准。
