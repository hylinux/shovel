执行 Milestone A / Phase 4：Capability Registry 和 Immutable Version Model。

只实现本阶段，不实现 ExecutionEngine、Proactive Service、Workflow Runtime 或 UI。

目标：

1. 实现 CapabilityDefinition。
2. 实现 CapabilityVersion。
3. 支持 Capability 类型：
   - function
   - skill
   - workflow
   - agent
4. 实现 Draft、Published、Deprecated、Disabled、Archived 状态。
5. Published Version 必须不可变。
6. 编辑 Published Capability 时创建新的 Draft Version。
7. 实现 CapabilityRegistry 或适配现有 Registry。
8. 实现：
   - create draft
   - update draft
   - validate draft
   - publish
   - get
   - get version
   - list
   - search
   - resolve exact version
9. CapabilityVersion 保存：
   - input schema
   - output schema
   - required permissions
   - permission boundary
   - risk profile
   - side-effect profile
   - approval requirement
   - allowed capabilities
   - dependency lock
   - connection requirements
   - provenance
10. 禁止通过用户输入动态 import。
11. 不允许 arbitrary Python、JavaScript、Shell、eval 或 exec。
12. 为 Dependency Lock 定义数据结构，但本阶段不实现 Workflow 执行。
13. 优先使用当前项目既有 Domain、Repository 和 SQLAlchemy 风格。

测试至少覆盖：

- Create draft
- Update draft
- Publish
- Published version immutable
- Create V2 draft from V1
- Resolve exact version
- Disabled capability not executable
- Draft not available for proactive execution
- Version uniqueness
- Definition and Version separation
- No dynamic import from user input

完成后运行：

- formatter
- linter
- type checker
- related unit tests
- related migration tests

停止条件：

汇总实际变更、测试结果、数据库变化、与计划偏差和遗留问题后停止。不要开始 Phase 5。