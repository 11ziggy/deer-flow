# DeerFlow AI 应用工程学习课程

本课程面向希望通过 DeerFlow 学习 AI 应用工程，并最终形成简历项目经历的开发者。

建议投入比例：

- 70%：Agent、工具、上下文和运行时后端
- 20%：评测、可靠性和可观测性
- 10%：前端流式交互

课程共 30 节，每节建议投入 2～3 小时。每节都应留下可验证产出，例如代码提交、测试、架构图、实验记录或技术文档；仅阅读代码不视为完成。

## 第一阶段：建立 AI 应用工程基础

### 第 1 课：AI 应用工程的完整技术栈

学习内容：

- AI 应用与传统后端、模型算法的区别
- “概率模型 + 确定性工程外壳”的基本思想
- Prompt、Tool、Memory、Workflow、Runtime、Eval 的职责
- DeerFlow 为什么定位为 Agent Harness

实践任务：

- 阅读 `README_zh.md` 中“从 Deep Research 到 Super Agent Harness”及核心特性
- 画出“用户—Agent—模型—工具—外部系统”关系图

课程产出：一页 DeerFlow 项目定位说明。

### 第 2 课：DeerFlow 系统架构

学习内容：

- Nginx、Frontend、Gateway、Harness、Sandbox 的分工
- `app` 和 `deerflow-harness` 的依赖边界
- Agent 框架与业务 API 分层的原因

实践任务：

- 阅读 `backend/docs/ARCHITECTURE.md` 
- 找到每个服务的启动入口、端口和依赖方向

课程产出：一张分层架构图。

### 第 3 课：环境、配置与运行模式

学习内容：

- `config.yaml` 和 `extensions_config.json`
- 模型、工具组和 Sandbox 配置
- flash、standard、pro、ultra 模式的区别
- 环境变量与密钥管理

实践任务：

```powershell
make doctor
make config
make install
make dev
```

使用四种模式执行同一个任务，记录模型调用、工具调用和 Sub-agent 行为。配置文件可能包含密钥，不要提交到 Git。

课程产出：运行模式对比表。

### 第 4 课：LLM API 与消息协议

学习内容：

- System、Human、AI、Tool Message
- Tool Calling 的请求与响应结构
- Token、上下文窗口和 finish reason
- 流式输出与普通请求
- 为什么模型输出必须被视为不可靠输入

实践任务：

- 调用一次 OpenAI 兼容接口
- 手工完成一轮“模型请求工具→执行工具→把结果交回模型”

课程产出：一个不依赖 LangChain 的最小 Tool Calling Demo。

### 第 5 课：LangChain 与 LangGraph 最小 Agent

学习内容：

- Model、Tool 和 Agent Loop
- State、Node、Edge 和 Reducer
- Checkpointer
- `invoke` 与 `astream`

实践任务：实现一个包含两个工具、一个状态字段和一个 Checkpointer 的命令行 Agent。

课程产出：可进行多轮对话并流式输出的最小 Agent。

## 第二阶段：读懂 DeerFlow 的 Agent 核心

### 第 6 课：追踪一次完整请求

学习下面的主链路：

```text
Frontend
→ Gateway Router
→ start_run
→ RunManager
→ run_agent
→ Agent Graph
→ StreamBridge
→ Frontend
```

实践任务：按顺序阅读：

1. `backend/app/gateway/routers/thread_runs.py`
2. `backend/app/gateway/services.py`
3. `backend/packages/harness/deerflow/runtime/runs/worker.py`

课程产出：一次请求的时序图。

### 第 7 课：Thread、Run、Checkpoint 与 Event

学习内容：

- Thread：长期会话状态
- Run：一次 Agent 执行
- Checkpoint：可恢复状态
- Event：执行过程记录
- 为什么这四种对象不能合并

实践任务：发送两轮消息，查询 Run、Checkpoint 和事件记录，标注它们的对应关系。

课程产出：核心数据模型术语表。

### 第 8 课：ThreadState 与 Reducer

学习内容：

- LangGraph 状态如何累积
- Messages、Artifacts、Todos、Goal、Delegations 的不同合并语义
- Reducer 错误为什么会导致重复消息或状态丢失
- Full 与 Delta Checkpoint

实践任务：阅读 `backend/packages/harness/deerflow/agents/thread_state.py`，给一个 reducer 补充边界测试。

课程产出：ThreadState 字段与 reducer 对照表。

### 第 9 课：Lead Agent 的组装过程

学习内容：

- 模型选择优先级
- Tool、Skill、Prompt 和 Middleware 如何组装
- Custom Agent 如何限制模型、工具和 Sub-agent
- `create_agent()` 最终接收了什么

实践任务：阅读 `backend/packages/harness/deerflow/agents/lead_agent/agent.py`，画出 Agent Assembly Pipeline。

课程产出：从配置到 Agent Graph 的构造图。

### 第 10 课：Agent Middleware

学习内容：

- `before_agent`、`before_model` 和 `after_model`
- 工具调用包装
- 中间件顺序为什么重要
- DeerFlow 的摘要、标题、记忆、循环检测和错误处理中间件

实践任务：实现一个小型中间件，例如统计每轮工具调用数量或注入运行元数据，并为它编写测试。

课程产出：第一个带测试的 DeerFlow 功能改动。

## 第三阶段：构建 Agent 的行动能力

### 第 11 课：Tool 的设计与实现

学习内容：

- Tool Schema 与参数验证
- ToolRuntime 和运行上下文
- 同步、异步工具
- ToolMessage 和错误处理
- 工具应当采用怎样的粒度

实践任务：实现一个真实小工具，例如项目依赖分析、Markdown 结构检查或 Git 变更摘要。

课程产出：带类型、错误处理和单元测试的 Tool。

### 第 12 课：Sandbox、文件与 Artifact

学习内容：

- Sandbox Provider 和 Sandbox 实例
- 虚拟路径与宿主路径映射
- acquire、lease、release 生命周期
- Upload、Workspace、Output、Artifact
- Local Sandbox 为什么不是安全隔离边界

实践任务：让 Agent 读取上传文件，在 Workspace 中处理，将结果写入 Outputs，并以 Artifact 形式返回。

课程产出：文件输入到文件输出的完整流程。

### 第 13 课：Skills 与渐进式上下文加载

学习内容：

- Skill 与 Tool 的区别
- `SKILL.md` 结构
- Skill 发现、显式激活和延迟加载
- `allowed-tools`
- 为什么不能一次把所有 Skill 放进上下文

实践任务：编写一个领域 Skill，测试自动发现、显式激活、工具权限限制和未激活行为。

课程产出：一个可复用、可测试的 Skill。

### 第 14 课：MCP 与外部系统集成

学习内容：

- MCP Client、Server 和 Transport
- stdio、HTTP、SSE 的差异
- 工具发现和 Schema
- MCP 缓存、会话和超时
- MCP 与直接调用第三方 API 的取舍

实践任务：接入一个简单 MCP Server，完成一次查询或写入，并观察 MCP 工具如何进入 Agent Tool Catalog。

课程产出：一项外部服务集成。

### 第 15 课：Custom Agent 与能力边界

学习内容：

- 专用 Agent 和通用 Agent 的取舍
- 模型、Prompt、Tool Group、Skill 和 Sub-agent 白名单
- 最小权限原则
- 配置驱动与代码驱动的边界

实践任务：创建一个只能使用指定 Skill、Tool 和模型的专用 Agent。

课程产出：领域 Agent V1。

## 第四阶段：复杂任务与上下文工程

### 第 16 课：Sub-agent 架构

学习内容：

- Lead Agent 为什么进行委派
- Sub-agent 上下文隔离
- Registry、Executor 和 Task Tool
- 并行任务的适用条件
- `execution_id` 与外部 `task_id`

实践任务：把一个任务拆成两个真正独立的子任务，比较串行和并行执行的耗时与 Token。

课程产出：Sub-agent 委派实验报告。

### 第 17 课：规划、Goal 与 Human-in-the-loop

学习内容：

- Todo 计划和模型内部推理的区别
- Goal 的跨轮持续状态
- Clarification 与 Interrupt
- 什么时候必须向用户提问
- 非交互任务为什么要禁用 Clarification

实践任务：实现一个包含“计划—工具执行—用户确认—继续执行”的工作流。

课程产出：带中断恢复的人机协作流程。

### 第 18 课：上下文预算与自动摘要

学习内容：

- 上下文膨胀的来源
- 摘要触发条件
- 最近消息与历史摘要的平衡
- Skill 和 Sub-agent 结果如何进入持久上下文
- 摘要错误如何累积

实践任务：构造长对话并触发自动和手动 `/compact`，比较压缩前后的 Token 与回答质量。

课程产出：上下文压缩实验。

### 第 19 课：长期记忆

学习内容：

- Conversation State 与 Long-term Memory
- Fact 抽取、去重、更新和删除
- 用户级与 Agent 级作用域
- 记忆污染与错误强化
- 写入门禁和召回策略

实践任务：设计五组多会话测试，验证用户偏好能否被正确保存、召回和纠正。

课程产出：记忆正确性测试集。

### 第 20 课：RAG 与对话引用

学习内容：

- RAG、Memory、Conversation Reference 的区别
- Chunk、Embedding、Retrieval 和 Rerank
- 检索结果如何进入上下文
- 引用、来源和权限隔离
- 检索正确率与最终回答正确率的区别

实践任务：为一个小型文档集建立检索流程，并设计可回答、不可回答、冲突文档和权限受限测试。

课程产出：一个小型 RAG 评测集。

## 第五阶段：运行时可靠性

### 第 21 课：Run 生命周期

学习内容：

- Pending、Running、Finalizing、Completed、Error、Interrupted
- Agent 执行为什么放到后台 Task
- HTTP 生命周期与 Agent 生命周期为何分离
- RunStore 与进程内状态

实践任务：阅读 `backend/packages/harness/deerflow/runtime/runs/manager.py`，画出 Run 状态机。

课程产出：Run 生命周期状态图。

### 第 22 课：SSE 流式协议与前端状态

学习内容：

- metadata、values、messages-tuple、custom、end
- 为什么同时需要状态快照和增量消息
- Sub-agent `task_*` 事件
- 前端节流和批量更新
- 乐观消息与服务端消息去重

实践任务：阅读 `frontend/src/core/threads/hooks.ts`，使用浏览器 Network 面板记录一次完整 SSE。

课程产出：SSE 事件样例与前端消费说明。

### 第 23 课：Checkpoint、恢复与取消

学习内容：

- 页面刷新后的流式重连
- Checkpoint 恢复
- Interrupt 与 Rollback
- 从历史消息重新生成
- 为什么取消后仍需要 Finalization

实践任务：测试运行中刷新、普通取消、回滚取消以及从旧 Checkpoint 重新执行。

课程产出：恢复行为对比表。

### 第 24 课：幂等、并发与多实例

学习内容：

- `Idempotency-Key`
- 同一 Thread 的 Run 冲突
- `reject`、`interrupt`、`rollback`
- 数据库唯一约束
- Worker ownership 和 lease
- 为什么进程锁不能解决多实例问题

实践任务：编写并发测试，同时向同一个 Thread 提交多个 Run，验证系统不会重复执行。

课程产出：并发与幂等测试。

### 第 25 课：错误处理、Guardrail 与可观测性

学习内容：

- 模型异常、Tool 异常和空回复
- Token Budget
- Loop Detection
- Safety Finish Reason
- Retry 与错误兜底
- RunJournal、RunEventStore、Token Usage 和 Tracing

实践任务：注入模型失败、工具失败、超时、循环和 Token 超限等故障，检查状态、用户错误信息和审计事件。

课程产出：Agent 故障分类表和故障注入测试。

## 第六阶段：完成简历级项目

### 第 26 课：AI 应用安全与部署

学习内容：

- Prompt Injection
- 工具越权
- 跨用户 Thread 访问
- 密钥进入 Metadata 或日志
- 文件路径穿越
- MCP 信任边界
- SSRF 与 Sandbox 逃逸风险
- 管理员权限为什么接近代码执行权限

实践任务：为自己的领域 Agent 编写 Threat Model。

课程产出：安全威胁、影响与缓解措施表。

### 第 27 课：Agent 评测方法与项目立项

学习内容：

- 为什么不能依赖主观体验评价 Agent
- 任务级成功率
- 确定性检查与 LLM Judge
- Token、延迟和工具错误率
- 失败分类
- 数据集版本管理
- Baseline、实验组和回归阈值

结课项目候选：

1. 代码仓库变更影响分析 Agent
2. 多源技术调研与决策 Agent
3. 企业文档知识助手
4. 数据分析与自动报告 Agent

课程产出：需求文档、架构图、评测指标和 20～30 个初始案例。

### 第 28 课：实现结课项目 MVP

MVP 至少包含：

- 一个 Custom Agent
- 一个自定义 Skill
- 一个自定义 Tool 或 MCP
- 文件或外部数据输入
- 结构化 Artifact 输出
- 至少一种上下文管理能力
- 端到端测试

课程产出：能够完整演示的 V1。

### 第 29 课：可靠性、评测与优化

实践任务：

- 扩充到 30～50 个评测案例
- 运行 Baseline
- 分析失败原因
- 优化 Prompt、工具描述或任务拆分
- 比较优化前后的成功率、Token 和延迟
- 加入取消、超时、重复提交和工具失败测试
- 运行格式检查与测试

课程产出：评测报告、真实指标、回归测试及 V2。

### 第 30 课：项目交付与面试准备

最终交付物：

- README：问题、方案、架构和启动方式
- 系统架构图与请求时序图
- 3～5 分钟演示视频
- 评测数据和优化前后对比
- 自动化测试结果
- 一篇技术总结
- 清晰的 Git 提交记录
- 如果条件允许，向上游提交一个独立 PR

需要准备回答：

- 为什么选择 LangGraph？
- 为什么使用 Agent，而不是固定 Workflow？
- 如何控制 Agent 的不确定性？
- 如何避免重复执行？
- 如何评估效果？
- 最难处理的故障是什么？
- 自己实现的功能与 DeerFlow 原有功能如何区分？

课程产出：可展示、可评测、可写入简历的完整项目。

## 阶段验收

| 课程节点 | 必须达到的结果 |
| --- | --- |
| 第 5 课 | 独立写出最小 LangGraph Agent |
| 第 10 课 | 能解释主请求链，并实现一个 Middleware |
| 第 15 课 | 完成 Skill + Tool/MCP + Custom Agent |
| 第 20 课 | 能设计上下文、记忆和检索方案 |
| 第 25 课 | 能处理流式、恢复、幂等和故障 |
| 第 30 课 | 拥有可评测、可演示、可写简历的二次开发项目 |

## 推荐学习节奏

- 每周完成 4～5 节，总周期约 6～8 周。
- 每节课建议按照“理论 30 分钟、代码追踪 45 分钟、实践 60 分钟、总结 15 分钟”推进。
- 每五节课进行一次复盘，没有达到阶段验收标准时先补齐，不急于进入下一阶段。
- 阅读项目时遵循“文档建立地图→代码确认事实→测试理解边界→Git 历史理解原因”。
