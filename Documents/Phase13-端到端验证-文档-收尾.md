执行 Phase 13：End-to-End Integration、Architecture Verification、Documentation 和 Final Validation。

不要在本阶段添加新的产品功能。

完成五个 Demo。

Demo 1：Daily Briefing

Manual：

Main Agent
→ InvocationService
→ Same Workflow

Scheduled：

ScheduleTrigger
→ Event
→ Proactive Service
→ InvocationService
→ Same Workflow

验证 Capability Trigger-Agnostic。

Demo 2：Important Email Assistant

Event：
email.received

Condition：
importance == high

Workflow：

Analyze Email Skill
→ Generate Draft Skill
→ Approval
→ Demo Send Email Function

Demo Send Email 不能发送真实邮件。

验证：

- external_write
- waiting_for_approval
- explicit approval
- authorization re-check
- resume
- success

Demo 3：User Skill

Analyze Customer Incident V1

验证：

- Manual
- Workflow
- Proactive indirect invocation
- Permission narrowing
- Version freeze

Demo 4：Version Immutability

Execution A 使用 Skill V1。

发布 V2 后：

- Execution A 继续 V1
- New Execution 根据版本策略解析具体版本
- Dependency Lock 不变化

Demo 5：User Control

通过 UI、CLI 和 Main Agent：

- Pause Service
- Change Approval Preference
- Set Quiet Hours
- Resume
- Explain Last Execution
- Pause All
- Resume All

验证所有入口状态一致。

逐条重新检查 15 条 Architecture Invariants，并输出最终 Compliance Matrix。

完成 ADR 和文档：

- Overall Architecture
- Capability Model
- Execution Model
- Permission Model
- Approval Model
- Proactive Service
- Workflow
- Event Reliability Boundary
- Schedule Trigger vs Wakeup
- Versioning and Dependency Lock
- Credential Reference
- Security
- Known Limitations
- Future Extensions

运行仓库适用的：

- formatter
- linter
- type checker
- backend unit tests
- backend integration tests
- migration tests
- CLI smoke tests
- API smoke tests
- frontend lint
- frontend type check
- frontend tests
- frontend build

不要删除或跳过测试来获得成功结果。

如有失败：

- 区分 existing baseline failure
- regression introduced by this work
- environment failure
- deferred limitation

最终输出：

1. Implemented architecture
2. Invariant compliance matrix
3. File changes
4. Database changes
5. API changes
6. UI changes
7. Test results
8. Security assessment
9. Backward compatibility
10. Known limitations
11. Deferred work
12. Recommended next milestone

完成后停止。