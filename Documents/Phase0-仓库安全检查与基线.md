执行 Shovel 统一 Capability、Execution 和 Proactive Architecture 项目的 Phase 0：Repository Safety Check。

本阶段只允许检查和记录，不允许修改业务代码。

请完成：

1. 检查当前 Git branch。
2. 检查 git status。
3. 列出未跟踪、已修改、已暂存文件。
4. 不覆盖、删除、回滚或格式化任何现有用户修改。
5. 检查仓库顶层目录结构。
6. 检查 Python、Node.js 和前端相关项目配置。
7. 识别当前 formatter、linter、type checker 和测试命令。
8. 在不修改代码的前提下运行适当的测试基线。
9. 如果完整测试成本过高，先运行最小 smoke test，并说明未运行部分。
10. 记录当前已经存在的失败，不要把基线失败归因于后续改动。

本项目必须遵守以下 15 条架构不变量：

1. Capability 永远不拥有 Trigger。
2. Proactive Service 不实现业务逻辑。
3. 只有一个持久化 Execution 模型，不创建 ProactiveTask、SkillTask、WorkflowTask 或 FunctionTask。
4. 只有一个 ExecutionEngine；Function、Skill、Workflow 和 Agent 是 Executor。
5. Published Capability Version 不可变。
6. Execution 开始前冻结所有 Capability Version，并使用 Dependency Lock。
7. Permission 与 Approval 相互独立，Approval 不能授予 Permission。
8. 权限沿调用链只能缩小，不能扩大。
9. 所有 External Side Effect 必须经过 Authorization、Risk、Policy 和 Approval Gate。
10. UI、CLI、API、Main Agent 和 Proactive Service 共用 Application Services。
11. Event 在 Execution 成功持久化之前不具备 Durable 保证，V1 不声称 Exactly Once。
12. Business Schedule Trigger 与 Execution Wakeup 必须分离。
13. Workflow Definition 不包含 Schedule、Event 或 Cron Trigger。
14. User-authored Skill 和 Workflow 不允许执行任意代码。
15. Main Agent 可以创建 Draft，但不能静默 Publish 或将普通自然语言当成高风险 Approval。

输出：

- Current branch
- Working tree status
- Uncommitted files
- Repository structure
- Technology stack detected
- Available verification commands
- Baseline test results
- Existing failures
- Safety risks

停止条件：

完成检查后立即停止。不要修改文件，不要创建架构代码，不要开始 Phase 1。
