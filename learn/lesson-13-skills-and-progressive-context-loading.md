# 第 13 课：Skills 与渐进式上下文加载

第 11 课的工具（Tool）让智能体（Agent）执行一个有参数和返回值的操作；第 12 课的沙箱（Sandbox）让它在受控工作区读取文件。本课回答：**当一个领域任务需要一套可复用的操作方法时，DeerFlow 怎样让 Agent 找到相关技能（Skill）、只在需要时读取说明，并让已激活 Skill 的工具声明发挥作用？** 它位于“运行时组装提示词与工具 → 模型选择工作方法 → 读取 Skill → 调用工具”这段请求主线。核心阅读约 35～45 分钟。

前置知识是第 9 课的 Agent 组装和第 10 课的模型、工具调用包装。必须掌握 Skill 与 Tool 的职责差别、`SKILL.md` 的最小结构、发现与激活的两条路径，以及 `allowed-tools` 生效的条件。MCP 的工具接入和 Custom Agent 的完整能力边界分别留到第 14、15 课；Skill 安装审核、密钥注入和跨实例热更新不进入本课验收。

## 从“做什么”到“怎么做”

假设用户要求“检查这批 Markdown 文档的标题层级并给出修改建议”。`glob` 可以列文件，`read_file` 可以读内容，`grep` 可以查标题；这些 **Tool** 是运行时提供的可调用操作，拥有工具名和参数结构（Schema），执行后返回结果。**Skill** 则是一个带元数据和正文说明的文件包：它告诉 Agent 何时适用、检查顺序、结果格式和需要按需读取的参考资料。Skill 本身不会注册一个新的 `glob`，也不会因为出现在目录里就自动执行正文；真正的文件读取和其他操作仍要经过现有 Tool。

这层区别解决的是复用工作方法的问题。同一组底层 Tool 可以用于检查 Markdown、分析代码或整理资料；若把每种方法都硬写进基础 Prompt，Skill 越多，请求越长，互不相关的步骤也越容易干扰本次选择。DeerFlow 因此先让模型看见可用 Skill 的**索引或元数据**，相关时才加载正文；正文引用的辅助文件也仅在需要时读取。这里的“自动发现”指运行时把候选 Skill 告诉模型，**不保证模型一定选中它**。

## 一个 Skill 从文件进入候选目录

下面是本课实践可用的教学示例，文件放在一个名为 `markdown-audit` 的 Skill 包目录下；这段示例不是仓库现有文件：

```markdown
---
name: markdown-audit
description: 检查 Markdown 标题层级并给出带文件路径的修改建议；适用于文档结构审查。
allowed-tools: "read_file glob grep"
---

# Markdown 结构检查

1. 先确认用户指定的文件范围；未指定时请求明确范围。
2. 用 `glob` 找到范围内的 Markdown 文件，逐个用 `read_file` 检查标题层级。
3. 只报告可由文件内容支持的问题：文件路径、当前标题、预期层级和建议。
4. 不直接改写文件；若用户要求修改，再按当前可用工具和用户要求执行。
```

`backend/packages/harness/deerflow/skills/parser.py::parse_skill_file` 从文件开头的 YAML 元数据区（frontmatter）读取 `name`、`description`、可选的 `allowed-tools`，并返回 `Skill` 元数据；缺少前两个非空字符串、YAML 无效或 `allowed-tools` 格式错误时返回 `None`。正文在这一步没有被塞进 `Skill` 的字段里。`Skill` 的文件路径和类别由 `backend/packages/harness/deerflow/skills/types.py::Skill` 保存，`get_container_file_path()` 形成 Sandbox 中的 `/mnt/skills/<类别>/<包名>/SKILL.md` 一类路径；实际根路径可由配置改变。

普通请求在 `backend/packages/harness/deerflow/agents/lead_agent/agent.py::_load_enabled_available_skills` 中取得已启用、且在当前 Agent 名单内的候选。底层的 `SkillStorage.load_skills()` 负责扫描、解析、合并启用状态、按需过滤和排序；`LocalSkillStorage._iter_skill_files()` 把含 `SKILL.md` 的目录当作包边界，不把包内部的另一个 `SKILL.md` 再注册为独立运行时 Skill。用户自建 Skill 由 `UserScopedSkillStorage` 放在用户目录下，公共 Skill 则来自共享目录。**文件存在、解析成功、启用、进入当前 Agent 候选名单**是依次收窄的条件，不能从“磁盘上有文件”推断“当前 Agent 可使用它”。`backend/tests/test_user_scoped_skill_storage.py::test_public_skill_package_children_are_not_registered` 与 `::test_two_users_isolated` 分别固定包边界和用户隔离。

## 发现只给线索；加载才给正文

DeerFlow 当前有两种候选呈现方式，开关是 `backend/packages/harness/deerflow/config/skills_config.py::SkillsConfig.deferred_discovery`，默认 `False`：

| 配置 | 基础提示给模型的内容 | 下一步 |
| --- | --- | --- |
| 默认 `False` | `<available_skills>` 中的名称、描述、类别和文件路径 | 模型认为相关时调用 `read_file` 读取 `SKILL.md`。 |
| `True` | `<skill_index>` 中的名称 | 模型先调用 `describe_skill` 获得描述和路径，再决定是否 `read_file`。 |

两种模式都**不在基础提示中放入所有 Skill 正文**。默认模式节省的是正文长度；延迟发现模式进一步节省描述和路径长度，但相关任务会多一次 `describe_skill` 工具往返。`backend/packages/harness/deerflow/agents/lead_agent/prompt.py::get_skills_prompt_section` 选择提示形态，`backend/packages/harness/deerflow/skills/describe.py::build_skill_search_setup` 建立 `describe_skill` 工具。`backend/tests/test_skill_describe.py::test_skill_index_no_description` 和 `::test_skill_index_no_location` 验证紧凑索引确实只含名称。模型是否觉得任务相关仍是模型决策，不是 `describe_skill` 替它选择。

一条自动选择路径可表示为：

```text
SkillStorage 扫描、解析、过滤
  → 基础提示列出元数据（或只列名称）
  → 模型选中相关 Skill；延迟模式先 describe_skill
  → 模型调用 read_file 读取 SKILL.md
  → ToolMessage 把正文带回本次对话
  → 模型依说明调用已有 Tool
```

`read_file` 成功后，`backend/packages/harness/deerflow/agents/middlewares/tool_error_handling_middleware.py::ToolErrorHandlingMiddleware._stamp_skill_read_metadata` 给结果附上已读 Skill 的路径和简短描述。`backend/packages/harness/deerflow/agents/middlewares/durable_context_middleware.py::DurableContextMiddleware._capture` 再从“模型发出的指定读取工具调用 + 配对成功的 ToolMessage”提取引用，写入 `ThreadState.skill_context`；单独的文字提到 Skill、读取辅助资源、失败读取、没有匹配结果的调用都不等于这次捕获。`backend/packages/harness/deerflow/agents/middlewares/skill_context.py::extract_skills` 中的两个判定如下，中间的工具调用 ID 配对代码已省略：

```python
if _tool_call_name(tool_call) not in read_names:
    continue
...
if metadata["path"] != expected_path:
    continue
```

第一处只接受配置的读取工具；第二处要求结果元数据路径与请求路径一致。`skill_context` 只保存名称、路径、描述和读取位置的引用，**不在该字段保存整份正文**；`backend/packages/harness/deerflow/agents/thread_state.py::merge_skill_context` 按路径去重并保留最近最多 8 个引用。它后续注入的是“曾加载过这个 Skill，需要时重新读取”的紧凑提醒；原先读取正文产生的 `ToolMessage` 仍可能留在消息历史中，直到历史被裁剪或压缩。`backend/tests/test_skill_context.py::TestExtractSkills::test_captures_skill_reference_with_description` 与 `::test_ignores_supporting_resources_under_skill_directory` 可复核捕获边界。

另一条是用户显式激活。若最新用户消息以 `/markdown-audit 检查 docs/` 开头，`backend/packages/harness/deerflow/skills/slash.py::parse_slash_skill_reference` 提取名称和剩余任务文本；`backend/packages/harness/deerflow/agents/middlewares/skill_activation_middleware.py::SkillActivationMiddleware._resolve_activation` 再核对 Skill 是否安装、启用、在当前 Agent 名单内，并安全读取正文。`SkillActivationMiddleware._prepare_model_request` 把正文作为隐藏的激活消息加入**本次模型请求**，无需模型先猜测或再次 `read_file`。该消息不写入图的普通消息状态；同一 Run 的后续模型调用使用运行上下文记住激活来源，避免每轮工具循环重复注入，而工具策略仍可继续依据该激活来源。`backend/tests/test_slash_skills.py::test_skill_activation_middleware_injects_hidden_human_context_for_model_call` 和 `::test_skill_activation_middleware_activates_once_across_tool_loop` 验证这两点。`/skill-name` 必须位于消息开头；保留的 `/help` 等控制命令不会被当作 Skill。

## `allowed-tools` 限制发生在何时

示例中的 `allowed-tools` 是 Skill 对**可用工具名**的声明，不是工具的实现，也不会凭空增加 Agent 原本没有的 Tool。`backend/packages/harness/deerflow/skills/parser.py::parse_allowed_tools` 接受空格分隔字符串或 YAML 字符串列表；已知的便携名称如 `Read`、`Grep` 会在字符串写法中映射到 DeerFlow 的 `read_file`、`grep`，列表写法保留原运行时名称。省略字段表示旧式未限制声明；显式空列表表示没有声明业务工具。别把 `Bash(git add *)` 之类参数模式当作已执行的参数级限制：当前策略按工具名判断，不检查实参。

真正应用策略的是 `backend/packages/harness/deerflow/agents/middlewares/skill_tool_policy_middleware.py::SkillToolPolicyMiddleware`。关键分支可压缩为：

```python
slash_path = read_slash_skill_source_path(context, owner_token=self._slash_source_owner_token)
if slash_path is not None:
    return _POLICY_SOURCE_SLASH, (slash_path,)
# 否则读取 state["skill_context"] 中的路径；
# 两者都没有则返回 passive，不过滤基线工具。
```

因此，仅“启用并被发现”的 `markdown-audit` **不会**收窄 Agent 原有工具。显式 `/markdown-audit` 在首个模型调用就激活其声明；自动路径则要等成功的 `SKILL.md` 读取被捕获后，影响后续模型调用。若一个 Run 有斜杠激活，它优先于同一上下文中另行读取的 Skill；没有斜杠来源时，多个已捕获 Skill 的显式工具声明取并集。只有已激活 Skill 都未写 `allowed-tools` 时保留旧式全工具行为；一旦有明确声明，未声明字段的 Skill 不会解除它。`backend/tests/test_skill_tool_policy_middleware.py::test_passive_enabled_skill_does_not_filter_lead_tools`、`::test_slash_activated_skill_filters_first_model_call_and_task`、`::test_loaded_skill_context_filters_follow_up_model_calls` 和 `::test_active_skill_union_and_legacy_semantics_are_preserved` 固定了这些分支。

策略一方面在 `wrap_model_call` 过滤模型可见的工具 Schema，另一方面在 `wrap_tool_call` 拒绝越界调用并返回错误 `ToolMessage`；因此“没在本次提示中看到某工具”和“实际调用被挡住”都有对应检查。`read_file`、`describe_skill`、`tool_search`、`review_skill_package` 在这一层策略的保留集合里，但仍须通过初始工具装配及其他授权；`task` 等业务能力要显式列出。每次有活跃策略的模型调用会按当前注册表重新核对路径、启用状态和 Agent 名单，所有活跃引用均无法解析时只保留框架工具。`backend/tests/test_skill_tool_policy_middleware.py::test_unauthorized_tool_execution_is_blocked` 和 `::test_explicit_empty_allowed_tools_keeps_only_framework_tools` 给出可复核行为。

这是一层**行为约束**，不要把它说成进程级安全隔离：它主要约束经该 Agent 中间件链的模型工具选择与执行；其他加载路径不一定被捕获，`skill_context` 也有容量上限。Custom Agent 名单和 Sandbox 文件投影是另一层边界，第 15 课再分析。

## 可选实践：把示例做成可验证的 Skill

可把上面的示例保存为 `{DEER_FLOW_HOME}/users/{user_id}/skills/custom/markdown-audit/SKILL.md`，或通过项目的 Skill 管理入口创建；公共 `skills/public/` 是仓库共享目录，不适合个人实验。不要把本机 `config.yaml`、`extensions_config.json` 或其他凭据提交到 Git。选一个只含两级标题的小型 Markdown 样本，用下面四组观察记录作为课程产出。自动路径涉及模型选择，允许“已发现但模型未选中”的结果；确定性验证应以提示、工具调用和测试断言分别取证。

| 要验证的条件 | 操作与预期证据 |
| --- | --- |
| 自动发现 | 默认配置检查 `<available_skills>` 中有名称、描述和路径；开启 `deferred_discovery` 后检查 `<skill_index>` 只有名称，`describe_skill` 可返回详情。两种情况均不应预载正文。 |
| 显式激活 | 以 `/markdown-audit 检查指定文件` 发起请求；首个模型请求含 Skill 正文，且同一 Run 的工具循环不重复注入。 |
| 工具限制 | 激活后让模型尝试使用未声明的 `bash` 或 `task`：其 Schema 不应出现在受限模型请求中；若仍构造调用，工具包装层应给错误结果。已声明的工具也须原本由 Agent 提供并通过其他授权。 |
| 未激活 | 在新的 Thread 中保持 Skill 已启用，但不加斜杠、也不读取 `SKILL.md`；确认 `skill_context` 为空，原有工具集合不因该 Skill 的 `allowed-tools` 被收窄。 |

离线复核可在 `backend/` 运行 `python -m pytest tests/test_skills_parser.py tests/test_skill_describe.py tests/test_slash_skills.py tests/test_skill_context.py tests/test_skill_tool_policy_middleware.py -q`。这些仓库测试证明解析、提示、激活、捕获和策略的实现边界；它们**不证明示例 Skill 在真实模型上必然被自动选中，也不证明 Markdown 检查建议的质量**。若要做个人 Skill 的回归测试，固定样本、请求文本和预期输出字段，再单独记录模型是否选择 Skill、是否遵循步骤及输出是否正确。

面试验收：能用 Markdown 检查例子说明 Tool 负责可执行操作、Skill 负责可复用方法；能从磁盘文件一路讲到候选提示、按需加载、`skill_context` 引用和工具策略；能解释默认与延迟发现的成本差别、斜杠激活与自动加载的差别，以及“已启用但未激活”为何不限制工具。下一课再看 MCP 怎样把外部系统的操作加入 Tool Catalog。
