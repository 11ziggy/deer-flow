# 第 14 课：MCP 与外部系统集成

第 13 课讨论 Skill 怎样把操作方法按需交给 Agent；第 11 课讨论单个 Tool 的接口。本课回答一个不同的问题：**外部系统已经提供工具时，DeerFlow 怎样发现它们、纳入 Agent 的工具目录，并在调用时守住连接和执行边界？** 它位于“Runtime 组装 Tool → 模型请求 Tool Call → 外部系统返回结果”这段主线。核心阅读约 35～45 分钟。

前置知识是 Tool Schema、Tool Call 和第 9 课的 `create_agent(tools=...)`。必须掌握 MCP 的三方职责、当前仓库的接入链、三种配置传输、目录缓存与 stdio 会话的区别，以及何时选择直接 API。第 15 课再收紧专用 Agent 的能力范围；跨用户权限模型、MCP Task 的持久化和故障恢复不在本课展开。

## 从一次“查询术语”看三方职责

假设用户问“查询外部术语库中的 MCP 定义”。模型本身不知道术语库的实时数据。模型上下文协议（Model Context Protocol，MCP）让一个**服务端**声明可调用工具及参数结构，让 **MCP 客户端**发现并调用这些工具；**传输**只负责客户端与服务端怎样交换协议消息。DeerFlow 在这里是宿主应用：它读取管理员配置、创建客户端、把发现的工具变成 LangChain Tool，再让 Agent 选择是否调用。术语库 MCP 服务端负责实际查询；模型提供商只处理 DeerFlow 最终绑定的工具定义并产出 Tool Call，不直接连接服务端。

```text
extensions_config.json 中启用服务端
  → DeerFlow MCP Client 连接并发现工具及参数 Schema
  → LangChain Tool 进入 DeerFlow Tool Catalog
  → Lead Agent 授权、处理延迟发现后交给 create_agent(tools=...)
  → 模型请求某个 Tool Call
  → DeerFlow 通过 MCP Client 调用服务端
  → 工具结果回到 Agent 的消息循环
```

协议中的 `tools/list` 给出名称、描述和输入 Schema，`tools/call` 携带名称与实参执行调用；这不等于“服务端给出的任何工具都应被信任或直接暴露给模型”。[MCP 工具规范](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)说明了这两个协议操作。DeerFlow 自己还会校验工具名、处理重名、应用授权以及可选的延迟工具发现。这是协议与宿主应用职责的分界。

## 传输选择影响部署和会话

`backend/packages/harness/deerflow/config/extensions_config.py::McpServerConfig` 接受 `stdio`、`http`、`sse`。`backend/packages/harness/deerflow/mcp/client.py::build_server_params` 先把配置转成适配器认识的传输参数，源码中的两行是：

```python
transport_type = config.type or "stdio"
params: dict[str, Any] = {"transport": transport_type}
```

同一函数随后要求 stdio 配置有 `command`，HTTP/SSE 配置有 `url`，并传递相应的参数；其他传输类型会被拒绝。配置缺 `command` 或 `url` 时，`build_servers_config()` 跳过该服务端；`backend/tests/test_mcp_client_config.py::test_build_servers_config_skips_invalid_server_and_keeps_valid_ones` 证明一个坏配置不会抹掉正常服务端。

| `type` | 连接对象 | 当前 DeerFlow 行为 | 适合的情形 |
| --- | --- | --- | --- |
| `stdio` | 客户端启动的本地子进程，经标准输入/输出通信 | 工具调用使用按用户、Thread 和事件循环隔离的持久会话池 | 本机命令或需要在同一会话中保留状态的服务端 |
| `http` | 远端 MCP HTTP 端点；这里对应 Streamable HTTP | 普通工具不走上述持久池，由适配器管理连接 | 独立部署的远端服务 |
| `sse` | 兼容旧式 HTTP+SSE MCP 服务端的 URL | 普通工具同样不走持久池 | 已有旧式服务端需要兼容时 |

这里的 `sse` 是 **MCP 客户端到服务端**的传输类型，不是第 22 课的 Gateway 到浏览器 Run 事件流。当前 [MCP 传输规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)列出 stdio 与 Streamable HTTP 两种标准传输；旧式 HTTP+SSE 是兼容路径。选择 `http` 时，服务端是否保持协议会话取决于它和适配器，不能把 DeerFlow 的 stdio 池化规则照搬过去。

## 工具怎样进入 Agent 目录

配置入口是根目录 `extensions_config.json` 的 `mcpServers`，示例在 `extensions_config.example.json`。`ExtensionsConfig.from_file()` 解析 `$ENV_VAR` 引用，`get_enabled_mcp_servers()` 只留下 `enabled` 的服务端；`build_servers_config()` 再转为适配器连接参数。`backend/packages/harness/deerflow/mcp/tools.py::get_mcp_tools` 使用 `MultiServerMCPClient` 逐个发现工具，单个服务端发现失败或超时会返回该服务端的空列表，其他服务端仍可加入。发现超时由 `session_init_timeout` 控制，默认 60 秒；`backend/tests/test_mcp_session_timeouts.py::test_discovery_timeout_skips_hung_server_without_blocking_healthy_server` 固定了这个边界。

发现后的工具仍要经过 DeerFlow 处理。`get_mcp_tools` 只接受名称匹配 `^[A-Za-z0-9_-]+$` 的工具；默认给可见名称加 `<server_name>_` 前缀以降低跨服务端撞名风险，`tool_name_prefix: false` 可以关闭。`backend/packages/harness/deerflow/tools/tools.py::get_available_tools` 随后把配置工具、内置工具、MCP 工具等合并，并按名称去重，前面的同名工具优先。最后，`backend/packages/harness/deerflow/agents/lead_agent/agent.py::_assemble_lead_agent` 对候选工具应用授权，再调用 `assemble_deferred_tools()`，把 `final_tools` 交给 `create_agent()`。因此“发现到工具”“进入 Agent 工具目录”“本次模型调用看见 Schema”“获准执行”是不同检查点；开启延迟发现时，目录中的某个 MCP 工具也可能暂时不向模型绑定。工具名校验见 `backend/tests/test_mcp_tool_name_validation.py::test_drops_only_the_invalid_tool_in_a_mixed_batch`。

工具结果也由边界代码转换：`mcp/tools.py::_convert_call_tool_result` 把 MCP 文本、图像、资源链接等内容转成 LangChain 可消费的内容块；`structuredContent` 放进 artifact；服务端返回 `isError=true` 时抛出 `ToolException`。这只说明 Agent 能收到结果或错误，不保证外部结果真实可信，也不保证模型最后答对用户问题。

## 缓存、会话与超时分别保护什么

**目录缓存**保存的是已发现的工具对象。`backend/packages/harness/deerflow/mcp/cache.py::get_cached_mcp_tools` 在首次需要时初始化，比较配置文件的已解析路径和 `(mtime, size, sha256)` 签名；配置变更时重建目录并退役旧会话池。`backend/tests/test_mcp_cache.py::test_same_mtime_same_size_swap_is_stale` 验证内容变化即使没有改变时间和大小也能被发现。这个检查针对**本地配置文件**，不能推断远端服务端的工具列表变化会在每次请求中自动刷新。

**stdio 会话池**保存的是已经初始化的连接及服务端会话状态，不等于工具目录缓存。`mcp/tools.py::_make_session_pool_tool` 用 `user_id:thread_id` 形成作用域，再由 `mcp/session_pool.py::MCPSessionPool.get_session` 加上服务端名和当前事件循环作为键。同一作用域内连续调用可复用状态，不同用户或 Thread 不能共享它；连接断开后会清理对应会话，后续调用可重建。`backend/tests/test_mcp_session_pool.py::test_session_pool_tool_reconnects_after_real_stdio_process_disconnect` 验证了重连路径。会话池有容量和退出清理成本，不能把它理解成无限期可用的远端任务存储。

**超时**分两个位置：`session_init_timeout` 约束发现与 stdio 会话初始化；`tool_call_timeout` 约束普通 stdio 工具调用。对普通 HTTP/SSE 工具，当前 `get_mcp_tools()` 会提示后者被忽略，应配置相应传输层的超时。超时只界定客户端等待时间，不证明外部写入没有发生；涉及有副作用的工具，是否重试还要看服务端的幂等语义。`backend/tests/test_mcp_session_timeouts.py::test_session_init_timeout_raises_when_session_creation_hangs` 和 `backend/tests/test_mcp_session_pool.py::test_non_stdio_tool_call_timeout_warns_that_it_is_ignored` 分别固定这两点。

## 什么时候直接调用第三方 API

MCP 的价值是把多服务端的工具发现与调用接进同一 Agent 目录，尤其适合已有 MCP 服务端、工具集合会演进，或需要在多个宿主之间复用集成时。它增加服务端部署、协议转换、连接状态和排障层次。如果只有一个稳定的 HTTP API，且需要精细控制请求、认证、事务或重试，直接写 DeerFlow Tool 往往更简单。MCP 服务端内部也可能仍在调用那个 HTTP API；MCP 并不会消除第三方认证、权限和数据可信度问题。

选择标准可以压缩为一句话：**先确定外部能力的信任与运行边界，再比较复用工具目录的收益是否超过多一层 MCP 服务端的成本。** 对真实服务端，应只启用可信来源，按用户或请求配置最小权限凭据，并把工具输出视为外部输入；仅靠模型提示不能代替服务端授权。更完整的威胁建模在第 26 课。

## 可选实践：接入一个本地查询服务

这个实验让外部查询进程通过 MCP 接入 DeerFlow，不需要第三方账户或模型额度。它验证**服务端声明 → 发现 → 目录 → 调用**，没有模拟真实第三方 API 的授权或稳定性。先按第 3 课准备好后端依赖及根目录 `extensions_config.json`。把下面内容保存为 `learn/outputs/lesson-14-lookup-server.py`；这段使用的是本仓库测试中同样采用的 `mcp.server.fastmcp.FastMCP` 和 `run(transport="stdio")` 接口：

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("lesson14-terms")
TERMS = {"mcp": "连接 AI 宿主与外部能力的模型上下文协议"}


@mcp.tool()
def lookup_term(term: str) -> str:
    """查询示例术语库中的一个术语。"""
    return TERMS.get(term.strip().lower(), "未收录")


if __name__ == "__main__":
    mcp.run(transport="stdio")
```

在自己机器的 `extensions_config.json` 的 `mcpServers` 中加入以下条目，保留其他现有配置。`command` 必须换成安装了后端依赖的 **Python 解释器绝对路径**，`args` 必须换成刚才脚本的 **绝对路径**；Windows JSON 路径可用 `/`，无需写双反斜杠。不要把含凭据的本地配置提交到 Git。stdio 服务端的标准输出只用于协议消息，调试信息应写到标准错误。

```json
"lesson14": {
  "enabled": true,
  "type": "stdio",
  "command": "C:/path/to/deer-flow/backend/.venv/Scripts/python.exe",
  "args": ["C:/path/to/deer-flow/learn/outputs/lesson-14-lookup-server.py"],
  "tool_name_prefix": true,
  "session_init_timeout": 10,
  "tool_call_timeout": 10
}
```

在 `backend/` 工作目录，用已安装后端依赖的 Python 运行以下检查脚本；它只使用当前 `extensions_config.json`，不需要模型凭据。`AppConfig` 的最小实例仅供检查候选目录，实际 Gateway 仍使用自己的 `config.yaml`。脚本中的两个断言分别检查服务端的参数 Schema 和 DeerFlow 的候选工具目录。

```python
import asyncio

from deerflow.config.app_config import AppConfig
from deerflow.mcp.tools import get_mcp_tools
from deerflow.tools import get_available_tools


async def check_call():
    tools = {tool.name: tool for tool in await get_mcp_tools()}
    tool = tools["lesson14_lookup_term"]
    schema = tool.args_schema
    assert isinstance(schema, dict)
    assert "term" in schema["required"]
    print(schema)
    print(await tool.ainvoke({"term": "MCP"}))


asyncio.run(check_call())
app_config = AppConfig.model_validate(
    {"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}}
)
assert "lesson14_lookup_term" in {
    tool.name for tool in get_available_tools(app_config=app_config)
}
print("已进入 DeerFlow 候选工具目录")
```

预期调用结果包含“模型上下文协议”。若未发现该工具，先检查是否启用、解释器和脚本绝对路径，以及发现失败或超时日志。如已有可用模型，再让 Agent 查询同一术语，查看是否产生该工具的 Tool Call 及工具结果。模型是否选择调用受提示、授权和延迟发现设置影响；脚本已能独立验证接入链。

课程产出是一页集成记录：配置的服务端名和传输类型、发现的工具名与 `term` Schema、一次调用的输入和结果，以及它在候选目录中的位置。若改接真实第三方服务，再补充凭据来源、权限范围与失败处理；不要求为完成本课从零实现业务 API。

面试验收：能沿上图讲清 MCP 服务端、DeerFlow 客户端、模型和 Agent 各负责什么；能解释为何“发现成功”不等于“可执行且被模型调用”；能根据服务端部署与状态要求选择 stdio、HTTP 或兼容 SSE；能区分工具目录缓存、stdio 会话池和两个超时，并说出直接 API 的一种更合适场景。
