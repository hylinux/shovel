# Shovel 默认配置参考（for Azure OpenAI）

本文针对当前 Shovel 实现：主聊天、Memory LLM、知识库 embedding 和 Memory
embedding 均可使用 Azure OpenAI、Azure CLI 自动令牌认证，也可分别选择 API Key。
Embedding 已通过 Microsoft Agent Framework 客户端生成向量。
以下配置不是 Ollama 配置，也不会自动修改现有用户的设置或索引。

## 1. 修正原配置中的重复键

`[model]` 和 `[memory.llm]` 内的 `security_key`、`api_version` 各只能出现一次。
TOML 不允许同一张表重复定义键；后面的空值不是覆盖前面的配置，而是解析错误。
两段都应保留：

```toml
api_version = "2025-04-01-preview"
security_key = ""
```

`max_input_chars = 240000` 是聊天模型的 Shovel 字符门槛，不是 240K token。
它不能扩大 Azure 模型的上下文窗口，也不能解决当前工具结果重复计数的根因。
实际部署名称及模型版本决定模型侧的 token 限制。

## 2. Embedding 模型选择

在下列三个模型中，质量优先推荐 **`text-embedding-3-large`**。
它适合作为中文、英文技术文档和 Memory 语义召回的起点；最终效果仍应通过
自己的检索问题验证，而不是只凭通用基准判断。

| 模型 | 默认向量维度 | 建议 |
| --- | ---: | --- |
| `text-embedding-3-large` | 3072 | 本参考的默认选择，质量优先。 |
| `text-embedding-3-small` | 1536 | 向量更小，适合偏重存储和吞吐的方案。 |
| `text-embedding-ada-002` | 1536 | 主要用于兼容既有系统；新配置不优先选择。 |

完整示例使用模型默认维度，省略 `dimensions`：large 对应 **3072**，
small 对应 **1536**。现在可在 `[embedding]` 和 `[memory.embedder]` 分别配置
`dimensions`，Shovel 会通过 MAF 将其传给 OpenAI 兼容 embedding API。
该值必须为正整数、与对应索引 `dim` 一致，且模型/API 必须支持它。
不能只改索引 `dim` 就期待模型自动改变输出；当前 Ollama 连接器不支持此参数。

若希望 `text-embedding-3-large` 输出 **1024 维**，在完整配置中调整以下字段
（合并到已有配置，不要重复添加同名 TOML 段）：

```toml
[zvec]
dim = 1024

[embedding]
dimensions = 1024

[memory]
dim = 1024

[memory.embedder]
dimensions = 1024
```

环境变量也可分别覆盖：`SHOVEL_EMBEDDING__DIMENSIONS=1024`、
`SHOVEL_MEMORY__EMBEDDER__DIMENSIONS=1024`。更改显式输出维度会更新向量指纹；
即使旧 `bge-m3` 也是 1024 维，换模型仍需重新生成知识和 Memory 向量，
不能混用旧向量。选择匹配的新索引目录，并按迁移章节重建。

Azure 配置中的 `model` 必须填写 **embedding 部署名称**，不是任意模型产品名称。
以下示例假设该部署确实名为 `text-embedding-3-large`；部署名称不同时必须修改。
聊天部署与 embedding 部署是不同的部署，不能互换。

## 3. 认证边界

| 用途 | 当前认证实现 | 配置字段 |
| --- | --- | --- |
| 主聊天 `[model]` | 空 Key 使用异步 `AzureCliCredential`，令牌缓存并按需续期；非空 Key 使用 API Key。 | `security_key` |
| Memory LLM `[memory.llm]` | 与主聊天使用相同的客户端工厂和认证规则。 | `security_key` |
| 知识 embedding `[embedding]` | 空 Key 使用 CLI 自动令牌；非空 Key 使用 API Key。 | `api_key` |
| Memory embedding `[memory.embedder]` | 与知识 embedding 使用相同的 MAF 适配器和认证规则。 | `api_key` |

下面的 embedding `api_key = ""` 会选择 CLI 认证，默认不需要 API Key。
客户端按需获取和缓存令牌，在请求时检查并自动续期；不会自行弹出登录。
如需 API Key，通过 `SHOVEL_EMBEDDING__API_KEY` 和
`SHOVEL_MEMORY__EMBEDDER__API_KEY` 提供非空值即可覆盖这个选择。
认证失败会明确报错，不会降级到另一种认证或切换模型。

CLI 模式要求 Host 以 Azure CLI 登录用户运行，且能在 PATH 找到 `az`。
该身份需要目标资源的 **Cognitive Services OpenAI User** 或等效推理权限。
CLI 登录不绕过资源防火墙、Private Endpoint 或 Shovel 的数据出口策略。

## 4. 完整默认配置参考

以下 TOML 可复制到自己的 `settings.toml`。不要把整个 Markdown 文件作为配置读取。

使用前替换 `YOUR-RESOURCE`、实际部署名称和本机路径；API version 必须是目标部署
仍支持的版本。这里沿用 `2025-04-01-preview`，不代表已验证你的 Azure 资源支持它。

**路径刻意使用新的 `C:\ShovelAzure` 数据目录，适用于新建环境，不是原有数据库的迁移。**
原工作区、SQLite 和向量索引不会因复制这个示例而被导入。需要保留原数据时，
先阅读后面的迁移注意事项，再决定哪些路径保留、哪些使用新索引目录。

```toml
service_name = "shovel"
agent_host = "127.0.0.1"
agent_port = 19566
http_host = "127.0.0.1"
dev_http_host = "127.0.0.1"
dev_http_port = 9999
dev_frontend_port = 3000
http_port = 8080
frontend_path = ""
log_level = "INFO"

[document]

[model]
model_provider_type = "azure_openai"
base_endpoint = "https://YOUR-RESOURCE.openai.azure.com"
model_name = "gpt-5.2-chat"
api_version = "2025-04-01-preview"
security_key = ""
max_input_chars = 240000
max_output_tokens = 1024
timeout_seconds = 300

[agent]
workspace_path = 'C:\ShovelAzure\workspaces\default'
enable_python_extensions = false
max_tool_iterations = 5

[performance]
warm_index = true
warm_local_models = true
max_foreground_runs = 0
foreground_queue_seconds = 120
background_idle_seconds = 5
embedding_cache_entries = 128
embedding_cache_seconds = 300
max_local_model_requests = 1
max_concurrent_scans = 5
context_profile = "default"

[database]
url = "sqlite+aiosqlite:///C:/ShovelAzure/data/shovel.db"
echo = false

[zvec]
dim = 3072
knowledge_path = 'C:\ShovelAzure\knowledge\shovel_chunk'
log_dir = 'C:\ShovelAzure\logs'

[embedding]
provider = "azure_openai"
base_url = "https://YOUR-RESOURCE.openai.azure.com"
model = "text-embedding-3-large"
revision = "azure-embedding-large-v1"
api_key = ""
api_version = "2025-04-01-preview"
batch_size = 8
timeout_seconds = 300
max_retries = 3
max_input_chars = 8000

[memory]
collection = "shovel_memory"
dim = 3072
index_path = 'C:\ShovelAzure\memory\zvec'
context_max_chars = 2000
max_items = 10
min_similarity = 0.55
min_confidence = 0.8
summary_max_chars = 2000
history_messages = 10

[memory.llm]
model_provider_type = "azure_openai"
base_endpoint = "https://YOUR-RESOURCE.openai.azure.com"
model_name = "gpt-5.2-chat"
api_version = "2025-04-01-preview"
security_key = ""
max_input_chars = 240000
max_output_tokens = 2048
timeout_seconds = 300

[memory.embedder]
provider = "azure_openai"
base_url = "https://YOUR-RESOURCE.openai.azure.com"
model = "text-embedding-3-large"
revision = "azure-embedding-large-v1"
api_key = ""
api_version = "2025-04-01-preview"
batch_size = 8
timeout_seconds = 300
max_retries = 3
max_input_chars = 8000
```

两个向量库可以使用同一 embedding 部署，但目录必须独立。
`base_endpoint` 和 `base_url` 均填写资源根地址，不追加 `/openai/v1`；
当前 Azure 客户端会组装带部署名称和 API version 的 URL。
示例的所有模型均在 Azure 上运行，配置生效后不再需要 Ollama 提供推理或 embedding。

Embedding 模型的单条输入限制约为 8192 tokens；`max_input_chars = 8000` 是字符限制，
不是 token 保证。不要将它改成聊天模型的 240000。特殊字符或较大的批次仍可能被
服务端拒绝；应减小单条字符预算或批次，而不是反复重试同一份无效输入。
现有适配器会截断超过该字符预算的 embedding 输入，原始 SQLite 文本保持独立。

`revision` 是 Shovel 的 embedding 身份标记，不是 Azure API version。
若同一部署名称背后的模型发生变化，应更改标记并重新生成向量。
更换模型后 Memory 的相似度分布可能变化，原 `min_similarity = 0.55`
只能作为起点，需要重新验证召回效果。

## 5. 环境变量与启动

先停止正在运行的 Host，备份配置，再更新依赖。Windows 环境使用 x64 Python：

```powershell
uv sync --python 3.14-x86_64 --locked --all-extras --dev
```

已有有效的 CLI 登录可继续使用；需要切换到资源所属租户时：

```powershell
az login --tenant YOUR-TENANT-ID
```

所有模型使用 CLI 时，不应设置覆盖空 Key 的
`SHOVEL_MODEL__SECURITY_KEY`、`SHOVEL_MEMORY__LLM__SECURITY_KEY`、
`SHOVEL_EMBEDDING__API_KEY`、`SHOVEL_MEMORY__EMBEDDER__API_KEY`。
同时取消 `AZURE_OPENAI_AD_TOKEN`，否则 CLI 模式会明确拒绝静态令牌配置。

**仅在希望 embedding 改用 API Key 时**，在启动 Host 的同一个 PowerShell
窗口中交互输入 embedding 资源的 Key；默认 CLI 配置不需要执行这段命令：

```powershell
$key = Read-Host "Azure OpenAI embedding API Key" -AsSecureString
$env:SHOVEL_EMBEDDING__API_KEY =
    [System.Net.NetworkCredential]::new("", $key).Password
$env:SHOVEL_MEMORY__EMBEDDER__API_KEY =
    $env:SHOVEL_EMBEDDING__API_KEY
```

这段命令假设两个 embedding 部署属于同一资源；分属不同资源时，
分别提供各自的 Key。不要将密钥写入参考文档、仓库或明文命令行参数。
这些变量只作用于当前进程及其子进程；换一个终端启动 Host 时需要重新提供。

配置好实际的默认 `settings.toml` 后检查并启动：

```powershell
uv run shovel config --check
uv run shovel init
uv run shovel run dev
```

默认配置文件位于 `%USERPROFILE%\.shovel\config\settings.toml`。
`init` 用于初始化配置和本地存储，不是 embedding 模型迁移或自动重新生成旧向量。
开发前端还需按根 README 完成 Node.js/npm 初始化。
完整重启 Host 才会加载新配置，仅点击 Restart Agent 不够。

配置优先级为环境变量高于 TOML，高于默认值。`AZURE_OPENAI_ENDPOINT`、
`AZURE_OPENAI_MODEL`、`AZURE_OPENAI_API_VERSION` 不会自动映射到 Shovel。
如需环境覆盖，使用 `SHOVEL_MODEL__BASE_ENDPOINT`、`SHOVEL_MODEL__MODEL_NAME`、
`SHOVEL_MODEL__API_VERSION`，以及对应的 Memory/embedding 字段。

## 6. 从 bge-m3 / 1024 维迁移

**不要只改模型名称、维度，然后继续使用原来的 1024 维 collection 目录。**
模型、维度或 embedding 身份变化后必须重新生成对应向量；即便两个模型维度相同，
向量空间也不相同，不能混用。

1. 停止 Host，备份原配置、SQLite、工作区及两个向量目录。不要删除旧数据库或 Memory 事实。
2. 若要保留原数据，保留原 SQLite 和工作区路径，为知识和 Memory 分别选择新的空 3072 维索引目录。不要让另一套 Host 同时写同一数据库。
3. 配置两个 Azure embedding 段及对应维度，确认 CLI 登录和推理权限（或提供 API Key），完整启动 Host。
4. 为所有相关资源/profile 重新生成知识向量。可对实际资源运行 `uv run shovel scan run RESOURCE --force`；不要加 `--no-embed`，也不要用限制文档数量的测试扫描代替完整重建。
5. 在 Settings 的 Memory 管理功能中执行向量索引重建，等待相关后台操作完成。切换目录本身不会自动把已有事实变成新模型向量。
6. 用已知文档和 Memory 事实验证检索、维度、操作状态及失败信息，再结束迁移。部分完成或有错误的任务不能当作成功迁移。

知识扫描只能重新获取当前连接器仍能读取的文档；源已不可访问时，
不能假定一次重新扫描就重建了全部历史向量。Memory 重建也需要 Azure embedding
服务可用。重建期间不要把关键词搜索成功当作新向量已全部就绪。

## 7. 验证与常见错误

先验证简单聊天，再分别验证一条知识文档的扫描/语义检索和一条 Memory 的索引/召回。
MCP 文档搜索成功不等于知识 embedding 或 Memory embedding 已配置成功。

| 情况 | 优先检查 |
| --- | --- |
| TOML 重复键错误 | 删除同一段内重复的 `security_key` 和 `api_version`。 |
| CLI 认证失败 | `az` 的 PATH、登录用户、租户、登录有效期及推理权限。 |
| Embedding HTTP 401/403 | CLI 身份/权限或 API Key 是否匹配资源，以及资源网络访问规则。 |
| DeploymentNotFound / HTTP 404 | 填写的是部署名称而非模型产品名称，地址是否对应部署所在资源。 |
| 不支持 API version | 使用资源/部署仍支持的版本，不要把版本字段留空。 |
| 预期 1024、返回 3072 等维度错误 | 配置维度、模型默认输出维度和 collection 的实际维度是否一致。 |
| Azure token 上下文超限 | 查看实际 token 用量；增加 Shovel 字符门槛不能扩大模型窗口。 |

聊天、Memory 提取/摘要、知识文档及 Memory embedding 的输入均可能发送到 Azure。
本地 SQLite/Zvec 存储不代表推理仍在本地；确认这些数据允许发往所配置的资源。
客户端迁移本身保留原有 embedding 身份和向量指纹；只更新软件、不更换模型时，
不需要重建索引。本文未进行真实 Azure 调用、登录、取 Key 或数据库迁移。

## 官方参考

- [OpenAI embedding 模型、默认维度和输入限制](https://developers.openai.com/api/docs/guides/embeddings)
- [Azure OpenAI embedding 部署与认证](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/embeddings)
- [Azure OpenAI Entra ID 认证与权限](https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/managed-identity)
