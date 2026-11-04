# Enterprise security threat model（ES-10）

## 范围和当前结论

本模型覆盖 Enterprise Security Foundation 的 contracts、silent authentication、
Protection kernel、metadata-only SQLite/isolated vector 基础，以及既有 Engine 的
synthetic whole-buffer read/display 路径。它不是 penetration test、合规认证或企业上线批准。
架构/实施证据见 [analysis](enterprise_security_analysis.md) 与
[ADRs](enterprise_security_adrs.md)；示例和接口指南见
[Provider guide](enterprise_provider_guide.md)。

**当前生产企业 admission 关闭**。后续已实现受控安装 native WAM 桥接及 H3 Kusto
元数据组织签名/观察组件，但尚未部署或通过真实组织认证验收。
H1 已接入受管 Host 的拒绝式 containment，见分析第 24 节；没有可信正向适配器的
企业模型/Artifact/clipboard/Memory/Knowledge 等出口继续拒绝。ES-10 示例本身仍只是离线证据；
不把现有个人聊天、MCP、上传、Memory/Knowledge 自动变成受保护企业入口。
H2 已在受管 Host 接入受信 Function 的 durable intent/receipt、未知效果隔离与显式
reconciliation/resume；生产写入 adapter 仍为空，没有真实企业写入或 Exactly Once 验收。
H4 已增加组织签名治理矩阵、存储/密钥证据门禁、metadata retirement/redaction 与隔离
SQLite WAL/backup、native vector 恢复验收。真实 storage/KMS authority 仍未安装；
正式组织矩阵与加密/介质擦除未验收，企业持久化继续拒绝，不能把绿色测试当作物理证明。

## 资产、攻击者和信任假设

| 资产 | 保护目标 |
|---|---|
| Token/password/cookie/claims challenge | 仅短时认证边界持有，不能进入业务值、日志、持久状态或 LLM |
| 原始/派生企业正文 | 每次用途与目的地都授权；不能因 READ、变换、本地处理而降级 |
| Identity/Connection/Manifest/model/policy 绑定 | 不可被 Agent/用户参数换 tenant、principal、revision、cloud、endpoint 或 authority |
| Execution/Approval/auth wait/deadline | 恢复不扩大预算/权限，不重复未知副作用，不把旧完成事实当新凭据 |
| source lineage/generation/authorization snapshots | 撤权、删除、过期、policy 变化能阻止旧引用/候选重新释放 |
| SQLite/audit/vector metadata | owner 隔离、完整性、受控读取与 retention；不冒充安全加密或擦除 |

攻击来源包括恶意 prompt/tool 参数、不可信文档、过期/重放请求、被替换的目标、
迟到/重复并发回调、错误 SDK 行为、source 撤权、日志/终端重定向和组件不可用。
工作区扩展、MCP、组织外模型与 source 返回值不天然可信。

内核、已审核的 handler/decoder/transport/policy adapter 和安装 catalog 是可信计算基。
可信 policy 必须来自组织授权的完整性/版本/撤销机制，个人设置只能收窄。
Python 进程**不是沙箱**：恶意同进程代码可以读取内存、调用私有成员、替换 callback；
当前方案不防同用户 malware、管理员、被篡改解释器/依赖或恶意安装者。
不能把 typed wrapper、frozen DTO 或 DPAPI 宣称为对这些攻击者的隔离保证。

## 信任边界与数据流

1. 人/Agent/Workflow 参数 → frozen Registry/Invocation/既有 Engine。授权在 Risk/Policy/
   Approval 前执行，dispatch 和恢复后再检查；child authority 只能收窄。
2. trusted catalog/profile resolver → TokenRequest/broker。精确 tenant/account/target/
   scope、silent mode、原 deadline；仅 AuthContext 到 trusted transport。
3. transport/source → bounded decoder/ProtectedContent。raw bytes 不直接进入工具结果、
   streaming、存储或模型；source metadata 与 provenance 必须完整且可信。
4. ProtectedContent → policy/audit/consumer。READ 不授予任何后续 action；
   whole-buffer release 前检查具体 destination、obligation、lease 与 source 新鲜度。
5. transient content → metadata/reference/isolated index。generated body 不可恢复；
   候选必须回查权威 SQLite/source，不允许 vector 命中直接成为证据或正文。
6. 状态/错误 → UI/通知/telemetry/audit。安全状态与 opaque refs 不是正文传输通道；
   audit 自身是敏感 metadata，需要独立访问与保留策略。

## 威胁、控制、证据与剩余风险

下列测试文件均位于 `backend\tests`；测试是受控路径证据，不是所有 adapter 的覆盖证明。

| 威胁 | 已实现控制与证据 | 剩余风险/上线阻断 |
|---|---|---|
| confused deputy：更换 tenant/audience/cloud/resource | exact immutable plan、Manifest/profile revision、operation digest、前后 re-resolve；connection/identity contracts、`test_enterprise_failures.py` | 真实组织 catalog/trust root、Provider resolver 未装配 |
| 用户 grant/Approval 被误当外部授权 | platform permission、auth、rights、data policy 独立检查；`test_enterprise_execution.py`/`test_protection_policy.py` | 真实外部授权/source observer 和全部工具出口仍待接线 |
| 静默失败自动换登录方式/冒用账号 | silent-only resolver/acquire；typed wait、原 deadline、fresh acquire；`test_enterprise_authentication.py` | ES-5 真实 WAM broker-required/non-fallback、Windows UI/取消未验收 |
| Token/SDK 异常泄漏到 Agent/log/SQLite | ephemeral single-use credential、safe exception/repr、固定 schema、所有表哨兵扫描；`test_enterprise_failures.py`/`test_enterprise_sample.py` | 真实 SDK logger/telemetry 必须各自验证，内存不防恶意同进程代码 |
| unlabelled ingress / prompt laundering | missing/unknown protection deny，source DATA lineage 及 decoder identity 检查；execution/protection tests | 既有 MCP/粘贴/上传无法可靠识别所有企业数据，不允许以“未经过 Provider”为豁免 |
| READ 后直接 model/embed/index/persist | 独立 permit；Host task 范围 containment、实际 AG-UI/model/tool/SQLite/embedding/index 前拒绝；`test_enterprise_egress.py` | 未纳管 ingress、可信正向企业模型/Artifact/clipboard、真实全链路验收仍未完成 |
| 派生结果洗掉限制/用旧 body 假恢复 | conservative merge、全部 DATA/CONTROL lineage、source-bound generation、transient process ownership；protection/persistence tests | 跨进程派生 body 恢复、实际模型输出 adapter 不在当前接受范围 |
| 撤权/删除/policy 变更后旧缓存再现 | query/materialization 回查、source fence、fresh snapshot、逐 binding generation；persistence/failure/vector tests | durable source-change admission/observer/delivery 尚未装配；信号未到达不能承诺即时撤权 |
| 重放 wait/completion/timer 或并发恢复 | atomic suspension、append-only completion、lease/claim、认证等待不被 timer 覆盖、原 deadline；execution/failure tests | 真实 broker、进程 crash 与组织外部系统仍需端到端验收 |
| write/display 后 crash 重复外部 effect | 原 Execution/attempt 上的 durable intent、稳定 key、metadata receipt、未知 effect 跨 key fence、只读 reconciliation 与单次人工恢复；`test_external_effects.py`、`test_platform_demos.py` | 真实 Provider 的 wire/幂等/终态合同、SDK 内部重试与认证 receipt 查询仍须验收；不声称 Exactly Once |
| 审计失败仍释放/DoS 无限等待 | explicit audit UoW、释放前 commit、bounded auth/deadline、audit failure tests | mandatory audit/retention/可用性/队列容量须组织定义；受控进程不是无限资源隔离 |
| 终端/屏幕/本地存储绕过 no-copy | DISPLAY 也按 destination/rights/obligation gate；未实现保护义务则拒绝 | 显示后不能靠按钮限制截图/redirect；没有通用受保护查看器 |
| SQLite/WAL/backup/vector 物理残留或篡改 | H4 签名矩阵、精确短时 storage/key proof、到期拒绝/metadata redaction、既有删除 outbox、离线 WAL 重打包与备份恢复检查；`test_enterprise_governance.py` | 实际加密、密钥系统、防旧 binary、审计生命周期和 native vector/介质擦除仍需可信后端证明；无生产 authority |
| 恢复旧 DB/备份撤销删除、key retirement 或 source fence | 恢复时必须查询恢复集外的当前 authority/inventory；外部单调退休先于本地提交，旧 generation/未注册或 pre-H4 数据拒绝 | 外部权威必须真实独立并防回滚；本地 SQLite tombstone/receipt 自身不是恢复授权，真实灾备未验收 |

## ES-10 示例额外约束

`scripts\demo_enterprise.py` 只使用 fixed synthetic 文本、无效 credential、
`.example.invalid` 和临时 SQLite，无 HTTP、模型、embedding、WAM 或用户 stores。
sample-specific publisher hash allowlist、enabled 内存副本和 connection policy 只存在
于它自己的 composition，不能导入生产 Runtime。持久配置仍 disabled/unavailable。
固定 logical clock 只为确定性演示，不证明真实 wall-clock/broker freshness。

示例 DISPLAY 是获准的 synthetic 终端 callback，不是日志通道或真实保护查看器。
派生 transform 只是固定纯内存计算；仅保留 metadata，EMBED 实际 DENY，无正文输出/
持久化或真实 sink 调用。DERIVE handler 不实现通用交互认证恢复；真实 read wait/resume
由现有 `EnterpriseReadHandler` 演示。临时目录删除只是测试清理，**不是安全擦除**。

## 未通过的上线门槛

| 门槛 | 仍需取得的证据 |
|---|---|
| 真实 WAM | 明确批准的 tenant/app/account，Windows broker-required/non-fallback、交互/取消/超时与组织策略实测 |
| H1 全出口 containment | 已接入现有受管出口的拒绝式 gate；仍需完整 ingress 与获准正向适配器、stream buffer/DLP、模型派生内容及真实组织全链路证据 |
| H2 外部副作用 | durable dispatch receipts、幂等/reconciliation、crash-window 与取消后的效果判定；禁止盲重试 |
| H3 组织 trust/lifecycle | 已安装组织 trust root/policy adapters、完整性/撤销、真实 source observer 与 durable delivery/admission |
| H4 retention/存储 | 组织批准的 retention/offline/revocation/delete 矩阵、访问控制、WAL/backup/vector 清理及加密/审计完整性策略 |

这些门槛未接受前保留 deny-default，不新增生产企业 enable 开关，不把离线 sample、
绿色单元测试或本机 local-first 当作组织授权和合规承诺。
