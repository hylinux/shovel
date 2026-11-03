# ES-5 WAM-required：独立路线 spike 与原生方案提案

**状态：Proposed，待确认；不是已实现或已通过的认证组件。**
用户已允许不登录、不启用生产的独立路线调查；尚未批准新增原生项目，
也没有获准的 tenant/app/account/scopes 测试环境。本文提交设计供确认，
不修改 [ENT-ADR-02](enterprise_security_adrs.md) 的生产决策。

## 1. 调查结论与证据边界

| 路线/公开证据 | 可以确认 | 不能据此接受 |
|---|---|---|
| [MSAL Python PublicClientApplication](https://learn.microsoft.com/en-us/python/api/msal/msal.application.publicclientapplication?view=msal-py-latest) | broker 初始化失败时可静默退回 non-broker；interactive callback 文档用途为 UI 通知 | `enable_broker_on_windows=True`、回调通知、成功 Token 或事后检查结果，不证明发送前 WAM-required |
| [MSAL.NET WAM guide](https://learn.microsoft.com/en-us/entra/msal/dotnet/acquiring-tokens/desktop-mobile/wam) / [BrokerOptions](https://learn.microsoft.com/en-us/dotnet/api/microsoft.identity.client.brokeroptions?view=msal-dotnet-latest) | 官方指南也明确 WAM 不可用时可回退 browser；页面所列 public BrokerOptions 不构成 required 保证 | 不能仅把 Python 换成 .NET、调用 `WithBroker()` 就宣布问题解决 |
| [WebAuthenticationCoreManager](https://learn.microsoft.com/en-us/uwp/api/windows.security.authentication.web.core.webauthenticationcoremanager?view=winrt-26100) | Windows 提供直接 WAM API；`GetTokenSilentlyAsync` 明确不显示凭据 UI | 尚未验证 Shovel 的 tenant/principal/cloud/scopes、注册、部署或运行兼容 |
| [Win32 interop](https://learn.microsoft.com/en-us/windows/win32/api/webauthenticationcoremanagerinterop/nn-webauthenticationcoremanagerinterop-iwebauthenticationcoremanagerinterop) | 桌面程序可通过公开 interop 将请求绑定 HWND | 没有可信窗口不应借用任意窗口或用 0 句柄继续；不能把 UWP 示例直接当作桌面实现 |
| [IAsyncInfo.Cancel](https://learn.microsoft.com/en-us/uwp/api/windows.foundation.iasyncinfo.cancel?view=winrt-26100) | 原生 async operation 存在取消入口 | 方法存在不证明原生 UI/SSO 工作已停止，必须实际验证完成状态、UI 和迟到结果 |

这不是证明上述 SDK 永远无法组合出合格方案；结论是**当前公开证据和已运行测试
尚不足以接受该保证**。不读取私有 `_broker`、修改 SDK 内部状态、事后丢弃 fallback
Token 或把 `asyncio.to_thread` 取消当成原生取消证据。

2026-11-02 重新以提交的独立 PEP 723 lock、离线缓存运行
`scripts\probe_windows_auth.py`：22 项离线检查通过，
`production_ready=false`、`authentication_attempted=false`。
uv 在独立脚本环境从缓存安装 16 个包；主项目依赖/lock 不变。
脚本没有构造 SDK client、访问账户或调用认证，也不验证 native WAM 初始化。
这次结果仍是离线 SDK/API 证据，不是新一轮真实 WAM 验收通过。

## 2. 提议下一步：直接 Windows WAM API 的独立原型

建议先评审一个 **Windows-only、auth-only、standalone** 的小原型，
通过公开 WinRT/Win32 WAM API 验证必要保证，而不是包装另一个默认允许 fallback 的
MSAL client。原型不是新的 Runtime、Provider、HTTP business proxy 或 Workflow。
尚不创建 `.csproj`、下载原生依赖、调用账户 API或运行任何真实认证。

拟优先评估 .NET Windows desktop + WinRT projection；具体 SDK/TFM、投影/interop、
包版本和锁定方式在批准原型后根据实际 build 验证选择，不在本文虚构已可用的包组合。
若需要 C++/WinRT，另行说明工具链差异，不同时维护两套实现。
初期限定 Windows 11 / x64 process；ARM64 上 x64 仿真、最低 Windows build、SDK 和
provider/DLL 兼容各自记录，不能由 Python x64 wheel 推断。

桌面 interop 文档列出其最低 build 要求；真实最低产品 OS、packaged/unpackaged app
identity、redirect/registration 与组织允许的部署方式仍需验证。
[UWP WAM guide](https://learn.microsoft.com/en-us/windows/uwp/security/web-account-manager)
可用于理解 provider API，但其 Store/应用身份及示例 scopes 不能直接成为桌面产品配置。

### WAM-only 结构约束

- 只从已批准的安装/配置读取 provider、tenant、client、cloud、audience、scopes 和精确
  account binding；不接受 Agent、CLI 或 IPC 提供任意 authority、URL、scope 字典。
- Token acquisition 的应用调用路径仅为公开 Windows WAM API。不实现
  system browser/embedded-browser fallback、Azure CLI、device-code、ROPC、client secret
  或自行发 HTTP OAuth 请求；不自动切换 provider/cloud/account。
- provider 缺失、注册/身份不兼容、无法验证 tenant/principal 或取消保证不足时返回
  typed blocked/unavailable。不能通过“先请求，再看 Token 是不是想要的账号”补救。
- Windows broker 自身可能使用受控 web-based UI；这不等于应用自行启动 browser
  fallback。证明应基于可信 API/调用路径和实际观察，不能只凭窗口外观。

`FindAccountProviderAsync(..., "organizations")` 不是 exact tenant 绑定，
显示名/邮箱也不是稳定 principal。`WebTokenRequest` 的 provider-specific properties
不自动提供经过证明的 audience/tenant/claims contract。必须先获得正式 provider
协议依据，并验证精确 account/client/scope/resource/authority 的映射；不能猜字段、
默认 `/common`、假设 Token 总是 JWT，或采用示例中的 `User.Read` 作为默认授权。
此项当前仍为 blocker，而不是“使用原生 API 已解决”。

### 两个独立阶段

1. **无认证原型**：锁定并编译 standalone 工具、验证 Windows API/interop/窗口生命周期，
   使用 synthetic account/result/error 做 binding、deadline、race、late-result 与 safe
   serialization 测试。任何真实 provider/account 查询另行明确允许；这阶段不枚举系统
   账户、不调用 silent/interactive token API、不导出 SDK/native diagnostics。
2. **获准环境实测**：仅在提供受批测试配置、完成路线门槛并单独确认每类真实操作后，
   验证 silent、interactive、cancel、timeout 与不可用情况。测试只获取批准用途的
   短时凭据，不调用 Graph/Kusto/M365 或其他资源读取正文。

## 3. 窗口、取消和秘密生命周期

原型拥有自己的最小可信 HWND，在当前交互 Windows user/session 中运行；
不拿任意 foreground/terminal/browser 窗口冒充 parent，不在 Windows Service/session 0
弹窗。interactive 必须由明确的人类动作触发，不能由 Agent 或 proactive 打开。
原型不得自动创建账号、授权新 scopes、通过 consent prompt 或移除 Windows/Office
账户；出现此类要求时交给用户/管理员按独立批准流程处理。

每次请求绑定 request/correlation、冻结 identity/connection/manifest revisions、
原 deadline 和取消 generation。保留原生 async operation，取消时调用原生取消入口，
等待并检查其状态，检查可见 UI 是否结束；cancel/timeout 后不交付任何结果。
SDK/OS 忽略取消而迟到返回时仍拒绝交付，记录安全状态；**这仅证明不交付，
不等于 native 操作或 UI 已停止**。不能靠杀 OS WAM/Office 进程模拟取消成功。
无法证明真正原生取消的路线保持未接受。

初期原型不跨进程返回 Token：凭据仅在原型内短时持有，不打印、不保存、不写报告/
stdout/stderr/audit，也不调用业务资源。报告只允许固定测试 ID、状态、计数和时长，
不包含账户标识、claims、scopes 内容、原始异常/SDK response 或 Token 衍生值。
不声称 .NET/WinRT immutable string 可可靠清零；减少引用/关闭请求不等于安全擦除。
应用不另建 Token/refresh-token cache，但 **OS WAM 自身可能缓存会话/凭据**；
真实测试授权必须说明该效果，不能承诺调用 WAM 完全不改变系统认证状态。

## 4. 后续生产组件边界：不在初期原型实施

原型通过也不自动接入 Python Runtime。还需修订/批准 ENT-ADR-02 的 trusted
installation、签名/更新/回退、launcher、caller identity 与私有 IPC 设计：

- Windows auth-only 子进程，不注册网络监听，不接收企业正文，也不代替 Provider
  发送业务 HTTP。普通 stdout/JSON/result 永远不携带 Token。
- 候选 per-launch inherited handle/private pipe；协议版本、nonce、长度、expiry、
  request binding、single-use 与 replay rejection 都须测试。不仅凭 SID/PID、
  loopback 或“签名的 python.exe”认定可信调用者。
- 若 SDK 必须返回短时 AT，只通过独立 secret channel 进入 trusted Identity adapter，
  随 Execution lease/取消/deadline 失效，再由现有 AuthenticatedTransport consume；
  不返回 refresh token，不写 Operation/auth request/checkpoint/audit。
- app disconnect 只失效 Shovel profile、lease 和应用引用，不静默删除 Windows/Office
  账号或全局 SSO 状态。父进程退出、子进程异常和 late result 都必须 fail closed。

同用户恶意进程、可改写的 Python checkout、管理员或被篡改的安装不是此原型能隔离的
攻击者；生产 trusted installation/caller 仍是 H3 门槛。未批准前不新增原生项目、
进程管理器、安装器、IPC endpoint、生产 resolver/broker 或企业 enable 开关。

## 5. 验收矩阵与停止条件

| 必须证明 | 无认证阶段 | 获准环境阶段 / 当前状态 |
|---|---|---|
| WAM-only、非 WAM 路径零 dispatch | synthetic provider unavailable、静态调用/依赖检查，无 fallback 实现 | Windows 实际不可用/不兼容条件下的受控证据，不能只看 Token 或 mock；未验证 |
| 精确 tenant/account/client/scopes/audience | 正式协议映射、拒绝变更/未知绑定 | home/guest、批准 tenant/cloud、实际 principal 绑定；暂无获准环境 |
| silent 不显示 UI | synthetic 状态映射、无自动 interactive | 真实 WAM silent 与交互需求，零未授权 UI；未验证 |
| interactive 人触发/可信 HWND | 自有窗口与 action/request 绑定、失效窗口拒绝 | 单独确认后由用户操作 OS 登录；未验证 |
| 真实 cancel/timeout/late result | generation/deadline/concurrency 不交付测试 | 原生完成状态、UI 关闭与实际迟到结果；未验证 |
| safe diagnostics | synthetic sentinels、报告/schema/异常测试 | 实际 SDK/native 日志与 OS 诊断的安全配置；未验证 |
| disconnect/restart/退出 | 应用失效与请求清理，不删 OS 账号 | WAM/session/cache 行为与组织允许的清理范围；未验证 |

任一 required gate 无法证明即停止推进，保持 blocked/unavailable；
不能默默放宽为 non-broker，不能把生成的模板、编译成功、mock 或离线 probe 当作实测。
Global 与 China、guest/CA/claims 挑战不能互相代替，未接受的环境应明确不支持。

## 6. CI 前置与本轮交付

[CI run 41418963177](https://github.com/hylinux/shovel/actions/runs/41418963177)
对应 `2b848ec`：Windows/Ubuntu 都在依赖同步时因索引覆盖使 `--locked` 要求重锁，
尚未运行后续测试。按用户确认的最小修复，CI/release 不再覆盖为 PyPI，
继续使用 `pyproject.toml` 与 `uv.lock` 声明的 Microsoft proxy；不升级/重锁依赖。
Windows/Linux 的 offline locked sync **dry-run**、lock check、YAML 与 whitespace
检查通过；dry-run 不等于在 Linux 安装/运行成功或远端 CI 已变绿。

93 个既有 Ruff 格式基线文件按用户要求不改，故不能宣称完整 CI 恢复。
本轮只交付依赖同步修复与本设计；真实 WAM、原生项目实现、生产 admission 以及
H1–H4 继续未接受。后续实施独立原型需要先确认第 2–5 节范围，真实认证仍需另行确认。
