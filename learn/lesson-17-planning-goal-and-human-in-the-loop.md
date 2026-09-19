# 第 17 课：规划、持续目标（Goal）与人工参与（Human-in-the-loop）

第 16 课讨论了把独立工作交给 Sub-agent。本课回答另一类问题：**一个跨多步、可能跨多轮的任务，怎样保持“正在做什么”，并在必须由人决定的地方停下来？** 这里的人工参与指 Agent 在必要处向用户提问，收到答复后继续。它位于“模型决定行动 → 工具执行 → ThreadState 与 Checkpoint 更新 → 后续 Run 继续”的请求主线中。核心阅读约 30～40 分钟。

前置知识是第 7 课的 Thread 与 Run、第 8 课的状态合并、第 10 课的中间件和第 11 课的工具调用。本课必须掌握 Todo、Goal 和 Clarification 各自保存什么、在哪个边界生效，以及人工回复怎样接回工作。取消 Run、回滚和从旧 Checkpoint 恢复的完整语义留到第 23 课；Goal 评估器的质量和成本不是本课的验收对象。

## 用一件事分清三种状态

假设用户要“阅读两份材料，整理发布说明；写入正式文件前让我确认”。Agent 可以先列出步骤，再用只读工具检查材料，最后提出写入确认。这里有三个不同问题：

| 机制 | 回答的问题 | 当前仓库中的载体 | 典型变化 |
| --- | --- | --- | --- |
| 待办计划（Todo） | 下一步做什么、哪一步完成了？ | `ThreadState.todos`，由计划模式中的 `write_todos` 更新 | `pending → in_progress → completed`，也可重写或清空列表 |
| 持续目标（Goal） | 整体完成条件是否已满足，当前 Run 结束后还需不需要继续？ | `ThreadState.goal`，写在 Thread 的 Checkpoint | 活跃目标经评估后清除，或保留目标并记录阻塞原因 |
| 人工澄清（Clarification） | 哪个必要信息或授权只能由用户提供？ | `ask_clarification` 的工具请求及其提问 `ToolMessage` | 当前图调用结束；用户答复在同一 Thread 发起下一轮 Run |

Todo 是可见的工作分解，不是模型的内部推理记录。即使模型心里“想过”三步，也不会自动产生可检查的 `todos` 状态。Goal 是整体完成条件，不是 Todo 的最后一项：待办全部标成完成并不能证明目标实现；反过来，目标可能已经有充分证据达成，而旧待办尚未更新。Clarification 是一次需要人的交接点，不负责替 Agent 规划或判断整体成功。

`backend/packages/harness/deerflow/agents/thread_state.py::ThreadState` 把 `todos` 与 `goal` 定义成两个独立通道。两个 reducer 的核心规则相同：本次写入为 `None` 时保留旧值，显式写入的新列表或 Goal 才覆盖旧值。`todos=[]` 可以清空计划；清除 Goal 则走 `backend/packages/harness/deerflow/runtime/goal.py::write_thread_goal(..., goal=None)`，直接从新 Checkpoint 的通道值中移除 `goal`。这说明两者都能跨图调用留在 Thread 状态里，但更新入口和用途不同。

## 计划如何进入执行循环

DeerFlow 在 `backend/packages/harness/deerflow/agents/lead_agent/agent.py::_create_todo_list_middleware` 中按 `is_plan_mode` 决定是否创建 `TodoMiddleware`；`build_middlewares` 再把它加入 Agent。`backend/packages/harness/deerflow/agents/middlewares/todo_middleware.py::TodoMiddleware` 继承 LangChain 的 `TodoListMiddleware`，由框架提供 `write_todos` 工具和基础提示，DeerFlow 补上当前项目的状态与提醒策略。模型提供商只返回工具调用意图，并不替应用保存待办或执行工具。

一轮可能是：模型调用 `write_todos` 写入三步 → 框架把工具结果并入状态 → 模型调用只读文件工具 → 完成一步后再次调用 `write_todos` 更新状态。第 8 课的 reducer 决定 `todos` 的新列表怎样覆盖旧列表。`backend/tests/test_todo_middleware.py::TestTodoMiddlewareAgentGraphIntegration.test_reuses_thread_state_todos_schema_in_real_agent_graph` 用真实编译的测试图证明：模型调用 `write_todos` 后，结果中的 `todos` 确实包含所写步骤，而不只是提示里出现了工具名。

计划不会保证每项都完成。`TodoMiddleware.after_model` 在模型想直接结束、列表仍有未完成项时，最多提醒两次并跳回模型；提醒作为本次模型请求的临时消息，不保存为普通对话。`test_completion_reminder_is_transient_in_real_agent_graph` 验证了提醒没有进入最终 `messages`，也验证达到上限后仍可能留下 `pending` 项。因此面试中不能把“有 Todo”说成“系统强制完成计划”。当摘要使早期 `write_todos` 调用退出消息窗口时，`before_model` 会根据仍在状态中的 `todos` 注入提醒；这只是让模型重新看见计划，不是重新执行旧步骤。

## Goal 如何跨轮保持完成条件

Goal 由用户设置为 Thread 范围的完成条件。Gateway 的 `GET/PUT/DELETE /api/threads/{thread_id}/goal` 分别读取、设置和清除它；设置时 `build_goal_state` 建立活跃目标，并由 `write_thread_goal` 写入 Checkpoint。设置或清除碰到该 Thread 正在运行会返回 409，避免与执行中的状态写入交错。见 `backend/app/gateway/routers/threads.py::get_thread_goal`、`set_thread_goal`、`clear_thread_goal`。

`backend/packages/harness/deerflow/agents/goal_state.py::GoalState` 保留目标文本、活跃状态、续跑计数与最近评估；它不是长期用户记忆。一次 Run 中，`backend/packages/harness/deerflow/runtime/runs/worker.py::run_agent` 先执行用户可见的一轮，然后调用 `_prepare_goal_continuation_input` 检查活跃 Goal。`backend/packages/harness/deerflow/runtime/goal.py::evaluate_goal_completion` 把可见的人类与助手消息送入一次单独的模型评估，并要求给出 `satisfied` 与有类型的 `blocker`。例如：

```text
目标：完成发布说明并经用户确认写入
可见证据：助手已在可见文本中说明草稿完成，等待用户确认写入
评估：satisfied=false；blocker=needs_user_input
结果：记录评估并停止自动续跑，等待下一轮用户输入
```

上面是**有可见助手说明时的教学示意**，不是一次真实模型输出。澄清工具的提问 `ToolMessage` 不属于评估器读取的可见人类/助手文本；worker 还要求 Checkpoint 中有持久的可见助手回合收据。若缺少该收据，它记录 `run_failed` 与 `no_durable_end_of_turn` 并停止续跑，因此提问不保证一定得到 `needs_user_input`。`backend/tests/test_goal_worker.py::test_goal_worker_stands_down_without_durable_assistant_receipt` 固定了这一边界。

代码中的关键判定是 `backend/packages/harness/deerflow/runtime/goal.py::should_continue_goal`：只有未满足且 `blocker == "goal_not_met_yet"`，并且续跑次数和无进展次数都未到上限，才允许再进一轮。可继续时，worker 写入带 `hide_from_ui` 的内部 `HumanMessage`，在同一个 Gateway Run 中再次调用 Agent 图；满足时清除 Goal。`backend/tests/test_goal_worker.py::test_goal_worker_returns_hidden_continuation_when_goal_is_unmet` 和 `::test_goal_worker_clears_goal_when_evaluator_is_satisfied` 分别固定这两个分支。默认最多 8 次自动续跑、连续 2 次无新可见助手证据则停下；`::test_goal_worker_stands_down_when_no_progress_repeats` 验证后者。目标仍可能保持活跃，不能把“停下”误认为“已完成”。

评估依赖可见对话证据，而不是直接检查文件系统或外部服务。工具实际执行成功、模型宣称成功和用户确认，是不同种证据；可见材料不足时评估会保守地给出 `missing_evidence`。因此 Goal 提供持续追踪与有限续跑，不是可靠的外部状态验收器。完整任务验收仍需相应工具结果或测试证据。

## 人工确认怎样让流程停住并接回

在例子中，读取材料所需的路径若已明确，Agent 可以先读；正式写入需要用户确认时，应提出具体问题、交代待写入的目标和范围，并等待答复。`backend/packages/harness/deerflow/tools/builtins/clarification_tool.py::ask_clarification_tool` 定义了 `missing_info`、`ambiguous_requirement`、`approach_choice`、`risk_confirmation` 和 `suggestion` 等提问类型；需要多个值时还可用一个结构化表单收集。提问依据是**继续执行缺少必要输入或授权**，不是每遇到普通实现选择都打断用户。

`backend/packages/harness/deerflow/agents/middlewares/clarification_middleware.py::ClarificationMiddleware._handle_clarification` 的实际返回形状是：

```python
return Command(
    update={"messages": [tool_message]},
    goto=END,
)
```

`tool_message` 包含问题文本、对应的 `tool_call_id`，以及供界面呈现的人类输入元数据 `artifact["human_input"]`。这是**结束当前图调用，留下可见提问**；这里没有调用 LangGraph 的 `interrupt()`，也不能据此推断 Run 被标成 `Interrupted`。用户提交答复时，前端 `frontend/src/components/workspace/chats/chat-page.tsx::handleSubmitHumanInput` 将答复构造成一条带 `human_input_response` 元数据的用户消息，通过 `sendMessage` 送入同一 Thread 的下一轮 Run。下一轮从 Thread 的消息与 Checkpoint 继续理解任务；澄清机制本身不会自动重放先前的只读工具调用。

停止前还有一个安全边界：模型可能在同一响应中同时请求 `ask_clarification` 和写文件。`ClarificationMiddleware.after_model` 会在交互模式下移除并行的其他工具调用，再让提问工具结束图；否则那些工具可能抢在答复前执行。`backend/tests/test_clarification_drop_siblings_graph_integration.py::test_mixed_clarification_batch_does_not_execute_siblings_or_loop` 验证并行的 `bash` 没有执行，模型也没有立即再调用一次。这个保护只针对**同一批**工具调用；已经执行过的操作不会因提问而回滚。由此也可推断：若产品必须严格保证“未授权不得写入”，写入工具还需检查授权状态，不能只靠模型选择先提问；Goal 的后续自动续跑同样不能代替这项检查。

需要区分三种“停”：Clarification 是 Agent 主动结束当前图调用并等下一条用户消息；Goal 评估的 `needs_user_input` 是停止本次自动续跑、保留目标状态；第 23 课的 Run 取消与 Checkpoint 恢复是运行时控制流。它们在界面上都可能看起来像“等待”，但写入的状态、后续入口不同。大纲所说的“带中断恢复”在这个案例中是业务上的暂停与续办，当前澄清路径并不会从挂起的图节点原地恢复。

## 没有人能回答的任务

定时任务没有同步答复人。Gateway 创建这类 Run 时在内部上下文设置 `non_interactive=True`，Lead Agent 组装工具时移除 `ask_clarification`，避免把后台工作卡在提问上。证据是 `backend/app/gateway/services.py::launch_scheduled_thread_run` 与 `backend/packages/harness/deerflow/agents/lead_agent/agent.py::assemble_lead_agent` 中的工具过滤；`backend/tests/test_gateway_services.py::test_non_interactive_context_override_is_internal_only` 验证外部请求不能自行设置该开关。

另有供内部非交互渠道使用的 `disable_clarification`：若工具仍发出澄清请求，中间件返回普通 `ToolMessage` 提示模型基于已有信息继续，而不返回 `Command(goto=END)`。它包括风险确认问题，因此同样只能由可信内部调用方设置，不能让普通客户端用它绕过用户确认。后台任务若确实缺少不可推断的输入，合理结果是报告阻塞与假设，而不是伪造人的同意。这个边界由 `backend/app/gateway/services.py::strip_internal_context_keys`、`merge_run_context_overrides` 和 `backend/tests/test_clarification_middleware.py::TestClarificationDisabled` 固定。

## 可选实践：拼出一次可核对的交接

在本地测试 Thread 或用假模型构造固定工具调用，完成下面一条流程。实践用于验证理解，不要求重新实现 DeerFlow 的 Agent Loop。

1. 设置一个明确 Goal，例如“根据两份指定材料生成发布说明，用户确认后才写入指定输出文件”；本轮启用 `is_plan_mode`，让模型通过 `write_todos` 记录“读取材料、整理草稿、等待确认并写入”。
2. 只让确认前的工具调用读取测试材料，记录工具结果与 `todos` 更新。到写入边界，调用 `ask_clarification(clarification_type="risk_confirmation", ...)`，问题中写明目标文件与拟写内容。
3. 检查该轮消息中有提问 `ToolMessage` 和 `human_input` 元数据；确认前输出文件不存在。若模型并行请求写入工具，验证它没有执行。记录此时的 `goal`、`todos` 和 Run 状态，不把三者合成一个“任务状态”。
4. 在同一 Thread 提交“同意写入”的答复，下一轮仍启用计划模式；让 Agent 写入测试输出、验证文件内容并更新 Todo。检查 Goal 是否因可见完成证据而清除；若未清除，记录评估器给出的 blocker，而不是手工宣称成功。

课程产出是一页人机协作流程记录：两轮 Run 的消息顺序、确认前后工具执行证据、`todos` 与 `goal` 的状态快照，以及“当前图调用结束”和“下一轮继续”的分界。没有模型凭证时，可以用假模型与临时目录重放相同序列；此实验的关键验收是**未获确认前没有写入**，以及用户答复后能沿同一 Thread 继续。实验通过只证明这条执行轨迹；生产级授权仍须由执行写入的边界校验。

面试验收：能用上述案例说明 Todo、Goal 和 Clarification 各解决什么问题；指出 `is_plan_mode`、`ThreadState.todos/goal`、Goal 评估与澄清 `Command(goto=END)` 的代码证据；解释为什么澄清不是 LangGraph `interrupt()`，为什么目标未满足不一定自动续跑，以及后台 Run 为什么不能依赖即时提问。
