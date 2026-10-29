# 开发环境搭建指南


## 文档信息

| 项目 | 内容 |
|---|---|
| 文档名称 | Shovel 开发环境搭建指南 |
| 项目名称 | Shovel AI Agent Studio and Runtime Platform |
| 文档类型 | 开发环境搭建 / 依赖技术说明  |
| 作者 | HongWei Guo |
| 版本 | Draft v1.0 |
| 推荐路径 | `prepare-develop-env.md` |
| 更新时间 | 2026/10/29 |
| 目标读者 | 项目开发者、架构设计者、后续维护者 |

---

## 1. 开发工具说明

目前主要的开发语言是`Python`, `Typescript`, `CSS`, `html`，依赖如下的开发工具：

- Python 3.14+
- Node.js  Next.js 
- Tailwind CSS

需要安装的工具：

- Python 3.14+
- Node.js 22.18+（22 LTS；CI 与发布流程使用 Node 22）
- Python uv

请使用官方文档安装上述工具。

## 2. 本地存储与模型依赖

默认开发不需要启动数据库容器、缓存服务或独立 Memory Server：

- SQLite 保存知识文本、会话、Session Summary、长期记忆及持久任务。
- Zvec 是进程内的本地向量索引。知识索引与 Memory 索引独立配置；
  Memory 索引丢失后，可以从 SQLite 权威记录重新生成。
- 原生 Memory 的提取、合并、生命周期和检索由 Shovel 管理，
  通过适配层与 Microsoft Agent Framework 集成。

对话模型在 `[model]` 配置；知识向量化使用 `[embedding]`，维度对应
`[zvec].dim`；记忆向量化使用 `[memory.embedder]`，维度对应 `[memory].dim`。
Memory 提取与摘要默认复用 `[model]` 的配置，也可以通过 `[memory.llm]`
指定独立模型。未配置 Memory embedder 时，SQLite 记忆存储仍可用，
但语义召回不可用，聊天会明确提示。

使用云端模型会将相关文本发送给模型服务，并可能产生费用；
Local-first 存储并不意味着云端推理在本地运行。
配置示例、上下文预算及索引重建说明见 [README 的 Native Memory 章节](README.md#native-memory)。

## 3. 代码准备：

只需克隆 Shovel。依赖的 `py-generic-host` 在项目及 wheel 元数据中固定到 Git
commit，安装时需要 Git 和 GitHub 网络访问，不依赖同级目录的可编辑 checkout：

```bash
git clone https://github.com/hylinux/shovel.git
```

然后进入 `shovel` 目录，并运行：

```bash
uv sync --locked --all-extras --dev
```

这一步安装锁定的开发依赖，不会生成发布 wheel。前端使用 `npm ci`；
打包需要先 `npm run build`，将静态文件放入 `backend/src/shovel/static`，
再运行 `uv build`。CI/Release 已自动完成并检查干净 wheel 的实际运行。

Zvec 是原生依赖。Windows 使用 x64 Python，包括 ARM64 机器上的 x64
仿真环境；当前锁文件不包含 Windows ARM64 原生 wheel。Windows 安装命令：

```powershell
uv sync --python 3.14-x86_64 --locked --all-extras --dev
```

不要把 Windows venv 与 WSL venv 混用。

然后尝试运行:

```bash
uv run shovel version
```

或者

```bash
uv run shovel version --verbose
```

初始化项目：

```bash
uv run shovel init
```

安装前端依赖并启动开发环境：

```powershell
Set-Location frontend
npm ci
Set-Location ..
uv run shovel run dev
```

浏览器访问 `http://127.0.0.1:3000`；FastAPI/AG-UI 使用
`http://127.0.0.1:9999`，前端代理 `/api/*`，不需要单独启动 Agent 端口。
在 Settings 查看 Memory 索引状态和待处理数量，必要时执行索引重建。
重建可能调用配置的 embedding 服务，不是无成本的目录初始化。

## 4. 创建本地 Ollama 模型示例

模型定义文件为 [Documents/shovel-chat-fast.Modelfile](Documents/shovel-chat-fast.Modelfile)。
安装 Ollama 并启动本地服务后，在 Shovel 仓库根目录执行：

```powershell
ollama pull qwen2.5:3b
ollama create shovel-chat-fast -f .\Documents\shovel-chat-fast.Modelfile
ollama show shovel-chat-fast
```

`pull` 会下载基础模型。`create` 基于该模型创建名为 `shovel-chat-fast`
的本地模型配置，不进行训练或微调；示例设置 `num_ctx = 8192`、
`temperature = 0.1`。Ollama 的上下文长度单位为 token，
与 Shovel 的 `max_input_chars` 字符预算不同。

完整 Shovel 配置见 [Documents/settings.example.toml](Documents/settings.example.toml)，
其中 `[model]` 和 `[memory.llm]` 的 `model_name` 均使用 `shovel-chat-fast`。

`Have Fun!`
