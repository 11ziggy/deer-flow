# 第 15 课：Custom Agent 与能力边界

一个通用 Agent 可以处理许多任务，但每次都要从大量模型、工具和工作方法中选择。若任务固定为“审查 DeerFlow Skill”，我们希望它只发现审查用 Skill，不做无关委派，并默认使用合适的模型。本课的核心问题是：**Custom Agent 怎样把通用 Agent 收窄为专用入口，哪些限制由运行时执行，哪些只是默认值或提示？**

本课位于请求主线的“Gateway 选择 Agent → Runtime 组装 Model、Prompt、Tool、Skill → 模型请求工具 → Runtime 执行工具”一段，是第 11～15 课行动能力的阶段验收。沿用第 9 课的组装过程，并承接大纲中的工具、Skill 和 MCP：工具是运行时可执行的操作，Skill 是按需加载的任务说明包，MCP Server 则能向 Agent 提供外部工具。核心阅读约 30～40 分钟。必须能解释下面各项的真实限制范围，判断一个“只能使用指定能力”的要求是否已被满足。Sub-agent 内部执行留到第 16 课，完整安全威胁分析留到第 26 课。

## 一个名字怎样变成专用 Agent

DeerFlow 的自定义 Agent（Custom Agent）是一个**带名字的配置与 `SOUL.md`**，不是另写一套 Agent Loop。Gateway 的 `backend/app/gateway/services.py::build_run_config` 在 `assistant_id` 指向自定义 Agent 时，把其名字写入运行配置的 `agent_name`；`backend/packages/harness/deerflow/agents/lead_agent/agent.py::_assemble_lead_agent` 再按用户加载 `AgentConfig`。最后仍由同一个 `create_agent()` 构图。文件存储模式下，定义在用户目录的 `agents/<name>/config.yaml` 与 `SOUL.md`；配置为数据库存储时，读取同一逻辑定义。创建 API 是 `POST /api/agents`，实现入口为 `backend/app/gateway/routers/agents.py::create_agent_endpoint`。

```text
assistant_id = skill-auditor
  → build_run_config 写入 agent_name
  → load_agent_config(name, user_id) + load_agent_soul(name, user_id)
  → 解析模型、候选工具、可用 Skill、委派范围和系统提示
  → create_agent(model, tools, middleware, system_prompt, state_schema)
```

这里的专用性来自**装配输入不同**。`SOUL.md` 被 `lead_agent/prompt.py` 放进系统提示，负责角色、目标和工作方式；它能指导模型，但文本本身不是工具权限检查。真正能否执行某项操作，还要看工具是否进入图、运行时策略是否放行，以及外部系统是否授权。

何时值得单独建 Agent？当同一类任务反复出现、需要稳定的工作说明和能力范围时，自定义 Agent 便于复用与检查。若任务只有固定的确定性步骤，可以直接用工作流；若任务类型尚不稳定，先用通用 Agent 观察真实需求。专用 Agent 会增加配置、测试和维护成本，也仍可能在其允许范围内选错工具。

## 五个配置项不是同一种“白名单”

`backend/packages/harness/deerflow/config/agents_config.py::AgentConfig` 定义了 `model`、`tool_groups`、`skills`、`allowed_subagents` 等字段。先看一份**示意配置**，其中模型名必须替换为你在 `config.yaml` 中实际配置的名称；它不代表当前仓库已创建该 Agent：

```yaml
name: skill-auditor
description: 审查已有 DeerFlow Skill 的质量
model: <已配置的模型名>
tool_groups: []
skills: [skill-reviewer]
allowed_subagents: []
```

对应的 `SOUL.md` 可以只写：“审查已有 Skill，给出带证据的发现和修改建议；不要替用户修改被审查的文件。”仓库已有 `skills/public/skill-reviewer/SKILL.md`，其 `allowed-tools` 声明了 `review_skill_package`，可与第 11 课的 Tool 和第 13 课的 Skill 组成一个领域 Agent V1。示例中的空工具组表示**不选取 `config.yaml` 定义的工具**，绝不表示整张图没有工具。

| 配置 | 当前实现中的作用 | 容易误判的地方 |
| --- | --- | --- |
| `model` | 自定义 Agent 的候选默认模型 | 本次运行的 `model_name` / `model` 可优先选择其他模型；模型授权也可能改选获准模型。 |
| `SOUL.md` | 增加领域说明与行为约束 | 提示词不会撤销已暴露的工具权限。 |
| `tool_groups` | 筛选 `config.yaml` 的 `tools[]` | 内置、MCP 及装配阶段追加的工具不受这个字段完整约束。 |
| `skills` | `None` 为所有已启用 Skill，`[]` 为没有，名单为指定 Skill 的发现与激活范围 | Skill 名单本身不会在构图时收紧普通工具目录；也不等于 Skill 已激活。 |
| `allowed_subagents` | `None` 为所有可用类型，`[]` 禁止委派，名单限制类型 | 非空名单不会主动开启委派；运行开关仍需允许。 |

其中模型选择有一段足以复核的源码关系，见 `agents/lead_agent/agent.py::_assemble_lead_agent`：

```python
agent_model_name = agent_config.model if agent_config and agent_config.model else None
model_name = _resolve_model_name(requested_model_name or agent_model_name, app_config=resolved_app_config)
model_name = _authorize_model_name(model_name, context=cfg, app_config=resolved_app_config)
```

所以 `model` 是默认候选，不是固定模型。候选顺序为本次运行指定、自定义 Agent 配置、全局第一项；未配置的模型名会回退到全局第一项。启用模型授权后，最终模型还可能因 `model:use` 被拒而改选。`test_lead_agent_model_resolution.py::test_resolve_model_name_falls_back_to_default` 固定了无效名称的回退；`test_agents_router_model_settings.py::test_create_rejects_unknown_model` 说明通过创建 API 提交未知模型会先被拒绝。两者处于不同入口，不应混为一谈。

工具组的边界也能由短代码直接看出。`backend/packages/harness/deerflow/tools/tools.py::get_available_tools` 先执行：

```python
tool_configs = [tool for tool in config.tools if groups is None or tool.group in groups]
builtin_tools = BUILTIN_TOOLS.copy()
```

同一函数随后还会按条件加入内置工具和已启用 MCP Server 的工具。Lead Agent 又在 `agent.py::_assemble_lead_agent` 中追加命名 Agent 的 `update_agent` 等候选工具，经过 `apply_tool_authorization()` 和延迟工具装配才得到 `final_tools`。这解释了为什么 `tool_groups: []` 只关掉一层来源。`update_agent` 可让普通命名 Agent 在对话中持久修改自己的配置；若领域边界要求长期固定，也要把这条配置变更路径纳入权限设计。Webhook 渠道运行会暂时不暴露它，普通聊天不适用这个例外。

Skill 的限制也分两步。`agent.py::_available_skill_names` 把配置名单转成集合，`_load_enabled_available_skills` 再与当前已启用的 Skill 求交集；`SkillActivationMiddleware` 拒绝名单外的显式激活。只有 Skill 被显式激活，或其文件被实际加载进上下文，`SkillToolPolicyMiddleware` 才按该 Skill 的 `allowed-tools` 收窄模型可见与可执行工具；框架保留 `describe_skill`、`read_file`、`review_skill_package`、`tool_search` 等必要工具。`test_lead_agent_skills.py::test_make_lead_agent_custom_skill_allowlist_does_not_activate_tool_policy` 验证了**仅配置 Skill 名单时，基础工具仍在图中**。因此示例的 `skill-reviewer` 需要在请求中实际激活，不能把“允许发现它”当成“所有轮次都按它限制工具”。

委派的限制更接近硬边界。`agent.py::_assemble_lead_agent` 计算 `requested_subagent_enabled and allowed_subagents != []`，空名单使 `task` 不进入可用工具；非空名单会写入运行元数据，`tools/builtins/task_tool.py::task_tool` 在执行时仍核对 `subagent_type`。`test_lead_agent_model_resolution.py::test_empty_allowed_subagents_disables_requested_delegation` 验证了请求开关不能放宽空名单。这里仅讨论父 Agent 能否委派，子 Agent 的上下文与调度属于第 16 课。

## 怎样理解“最小权限”

最小权限原则是让执行一次任务所需的能力尽量少，并在**执行边界**核对权限。以上配置可以显著减少 Agent 看到的 Skill、配置工具组和 Sub-agent 类型，却不能单独证明“只能调用 `review_skill_package`，只能使用配置的模型”。对于严格要求，需要把允许的工具名应用于**全部最终候选工具**，覆盖内置、MCP、延迟发现和 `update_agent`，并在工具调用时再次检查；模型也要在运行入口或授权策略中限制实际可用集合。当前 `apply_tool_authorization()` 仅在 `authorization.enabled` 为真时过滤工具，不能把一个尚未启用或尚未配置相应策略的授权点当作现成保证。外部系统的凭据与资源权限仍由对应系统执行检查。

这也是配置驱动与代码驱动的分界：选择已有能力、设置角色和默认行为，优先用 Custom Agent 配置；若产品要求**每轮、每条入口都不能越过某个 Tool 或 Model 集合**，就必须核对并补足运行时的授权或装配代码，写拒绝用例，不能只加强 `SOUL.md` 措辞。配置的可编辑者、`update_agent` 与创建/更新 API 也决定了谁能改变这条边界。

## 可选验证：形成领域 Agent V1

在已有环境里启用 `skill-reviewer`，选一个真实存在的模型名，用 Web 的 Agent 创建界面或 `POST /api/agents` 提交上述字段与 `SOUL.md`。新开一个选择该 Agent 的会话，发送 `/skill-reviewer 请审查这个 Skill`，使用一个你有权访问的测试包。记录实际选中的模型、可用 Skill、出现的工具调用和结果证据；不要仅凭最终文字判断权限是否生效。

再做三个对照：

1. 请求名单外的 Skill，预期显式激活被拒；把 `skills` 改为 `[]` 后，已启用 Skill 也不应被发现或激活。`test_lead_agent_skills.py::test_make_lead_agent_empty_skills_passed_correctly` 可作离线复核入口。
2. 在请求中打开委派，仍应因 `allowed_subagents: []` 无法调用 `task`。这验证的是委派边界，不依赖真实子任务成功。
3. 检查图的最终工具名，而不是只检查 `tool_groups` 的返回值。如果仍有内置或 MCP 工具，记录为当前配置能力的边界；需要严格 Tool 白名单时，另设计覆盖最终目录和执行路径的授权测试。

这项实践的课程产出是**领域 Agent V1 的配置、一次成功审查记录、一份允许与拒绝能力表**。时间有限时可以只阅读正文与上述测试；从零编写授权系统不属于本课必做。愿意深入实践的学习者，可用假模型和假工具为自定义授权策略写“允许的工具可执行、其他工具不可见也不可执行、请求换模型被拒、Agent 无法自改策略”四类测试。不要把未通过的测试写成现有 DeerFlow 的保证。

面试验收：能用两分钟解释 Custom Agent 为什么仍使用 Lead Agent 图；能逐项说清 `model`、`SOUL.md`、`tool_groups`、`skills`、`allowed_subagents` 的作用与限制；面对“这个 Agent 只能调用指定工具和模型吗”，能依据上述源码和测试给出有条件的判断，并指出要在哪个执行边界补检查。下一课再分析一次实际委派怎样创建独立的 Sub-agent 执行。
