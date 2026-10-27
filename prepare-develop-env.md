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
| 更新时间 | 2026/7/31 |
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

## 2. 依赖的外部数据库等工具：

记忆子系统默认使用嵌入式 Qdrant（`memory.qdrant.mode = "local"`，一个目录就是一个库），
因此本机开发不需要额外启动任何服务。

只有当你把 `memory.qdrant.mode` 改成 `server` 时，才需要一个独立的 Qdrant。
本机开发建议使用 Docker Desktop，启动命令参考：

- `Qdrant`:

```bash
docker run -d --name qdrant_latest -p 6333:6333 -p 6334:6334 -v "d:/qdrantDB/:/qdrant/storage:z" qdrant:latest
```

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

Zvec 是原生依赖。Windows 开发应在支持该 wheel 的 WSL/Linux 环境中执行
Python 安装、测试与运行；不要把 Windows venv 与 WSL venv 混用。

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

`Have Fun!`

