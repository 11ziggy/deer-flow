# 第 3 课：环境、配置与运行模式

> 主题：从“把项目跑起来”走向“理解每一项配置改变了什么、何时生效，以及不同运行模式如何影响 Agent 行为”  
> 建议用时：3～4 小时  
> 本课性质：环境搭建 + 配置阅读 + 对照实验 + 故障诊断  
> 最终产出：一份可复现的环境检查记录 + 一张四种 Agent 模式对比表

> **本节课的核心不是背诵 `config.yaml`，而是建立四个判断：**
>
> 1. **配置由谁读取？** 是 Frontend、Gateway、Harness、MCP 进程，还是 Sandbox；
> 2. **配置改变了什么？** 是模型、工具、执行边界、持久化方式，还是界面行为；
> 3. **配置何时生效？** 是下一次请求立即生效，还是必须重启 Gateway；
> 4. **“模式”指什么？** 是服务启动模式、Sandbox 隔离模式，还是 Agent 工作模式。

如果不能回答这四个问题，即使应用暂时成功启动，也很难稳定地调试、部署和解释系统行为。

---

## 1. 为什么第三课要专门学习环境与配置

前两课已经建立了两层认识：

- 第 1 课解释了 Agent、Harness 和确定性工程外壳；
- 第 2 课解释了 Frontend、Gateway、Harness、Sandbox 和 Nginx 的边界。

但知道架构图，不等于能够把系统可靠地运行起来。真实项目中最常见的问题往往不是算法错误，而是运行环境和配置不一致：

- 本地终端里有 API Key，Docker 容器里却没有；
- `config.yaml` 写了一个模型，但 `use` 指向了错误的模型类；
- 页面选择了 Ultra，模型却不支持思考模式；
- Agent 有 `bash` 工具，Sandbox 却禁止执行宿主机命令；
- 修改了 `sandbox.use`，但没有重启 Gateway；
- 把本地地址 `127.0.0.1` 原样写进容器配置，结果容器连接的是自己；
- 把 Agent 的 Pro 模式误认为服务的生产启动模式；
- 为了方便，把真实密钥直接提交到了 Git。

这些问题有一个共同点：**同一份代码在不同配置、不同进程边界和不同启动方式下，会表现出不同能力。**

因此，本课的目标不是“成功执行一次 `make dev`”，而是理解下面这条因果链：

```text
环境变量与配置文件
        ↓
配置解析与校验
        ↓
模型、工具、Sandbox 和运行时被组装
        ↓
Frontend 把 Agent 模式映射为请求上下文
        ↓
Lead Agent 以不同的思考、规划和委派能力执行任务
        ↓
日志、事件、Token 用量和产出文件形成实验结果
```

当这条链路清楚以后，你才能回答：一次任务为什么失败、应该改哪里、改完是否需要重启，以及如何证明修复有效。

---

## 2. 本课学习目标

完成本课后，你应该能够：

1. 说明 `config.yaml`、`extensions_config.json`、根目录 `.env` 和 `frontend/.env` 的职责差异；
2. 说明 DeerFlow 如何定位主配置文件和扩展配置文件；
3. 阅读一条模型配置，解释 `name`、`use`、`model`、`api_key`、`context_window` 和能力声明；
4. 解释工具组和工具实例之间的关系；
5. 区分 Local、AIO/Docker、Provisioner 等 Sandbox 方案的能力与风险；
6. 区分开发启动、生产启动、Docker 部署和 Agent 工作模式；
7. 准确说明 `flash`、`thinking`、`pro`、`ultra` 对请求上下文的映射；
8. 使用环境变量引用密钥，并确认密钥文件不会被 Git 跟踪；
9. 判断一项配置修改是热加载，还是需要重启 Gateway；
10. 用同一个任务对比四种 Agent 模式，并用证据而不是主观感受得出结论；
11. 使用 `make check`、`make doctor`、日志和健康检查定位启动问题；
12. 产出一份别人可以复现实验的运行模式对比表。

---

## 3. 建议学习安排

| 阶段 | 建议时间 | 任务 |
| --- | ---: | --- |
| 概念建立 | 25 分钟 | 区分三类“模式”和四类配置载体 |
| 环境检查 | 30 分钟 | 检查 Node.js、pnpm、uv、Nginx 或 Docker |
| 配置阅读 | 60 分钟 | 阅读模型、工具、Sandbox 和环境变量配置 |
| 启动验证 | 30～45 分钟 | 完成配置、安装依赖、运行 Doctor、启动服务 |
| 模式实验 | 60 分钟 | 用同一个任务测试四种 Agent 模式 |
| 总结验收 | 30 分钟 | 整理对比表、回答自测题、保存证据 |

建议第一次学习时只配置：

- 一个可用模型；
- 基础文件读写工具；
- 一个明确的 Sandbox；
- 默认的本地数据库。

不要一开始同时启用多个模型提供商、多个 MCP Server、外部数据库、IM Channel 和复杂 Sandbox。变量越多，失败时越难确定原因。

---

## 4. 先分清三类“模式”

“模式”是本课最容易产生误解的词。在 DeerFlow 中，至少有三类彼此独立的模式。

### 4.1 服务启动模式

服务启动模式回答的是：**Frontend、Gateway 和 Nginx 以什么方式运行？**

| 启动方式 | 主要命令 | 典型用途 | 是否热更新 |
| --- | --- | --- | --- |
| 本地开发 | `make dev` | 日常开发、调试 | Gateway 和 Frontend 支持开发态热更新 |
| 本地生产 | `make start` | 验证优化构建、接近生产的本机运行 | 否 |
| Docker 开发 | `make docker-start` | 用容器运行开发栈 | 取决于 Compose 开发配置 |
| Docker 生产 | `make up` | 构建并启动生产 Compose 栈 | 否 |

这些命令决定的是**服务如何启动**，不决定 Agent 是否思考、是否规划、是否使用 Sub-agent。

### 4.2 Sandbox 隔离模式

Sandbox 模式回答的是：**模型请求执行命令或读写文件时，动作在哪里发生？隔离边界是什么？**

常见选择包括：

- `LocalSandboxProvider`：动作由 Gateway 所在环境处理；
- `AioSandboxProvider`：为任务使用独立的 AIO 容器；(Gateway 之外的独立 Sandbox 容器)
- `AioSandboxProvider + provisioner_url`：由 Provisioner 在 Kubernetes 中创建 Sandbox Pod；
- 其他远程或微虚拟机 Provider：例如 E2B、BoxLite、OpenSandbox、Tenki，具体能力以当前配置模板为准。

Sandbox 模式决定的是**工具动作的执行边界**，不等于 Agent 的 Flash、Pro 或 Ultra。

> 沙箱主要解决的是工具具体在哪里执行的问题。
>
> 隔离边界：如果模型生成了一条错误或危险命令，这条命令最多能接触到哪些文件、网络、进程和凭证？

### 4.3 Agent 工作模式

Agent 工作模式回答的是：**一次请求是否启用思考、任务规划和 Sub-agent 委派？**

当前 Frontend 和 Gateway 接受四个模式值：

```text
flash
thinking
pro
ultra
```

课程总纲中的 `standard` 是过时名称。当前代码和 API 校验使用 `thinking`，不存在可提交的 `standard` 模式。学习和实验必须以当前代码为准。

### 4.4 三类模式可以任意组合

例如：

```text
服务启动模式：make dev
Sandbox 模式：LocalSandboxProvider
Agent 工作模式：ultra
```

也可以是：

```text
服务启动模式：make up
Sandbox 模式：AioSandboxProvider
Agent 工作模式：flash
```

因此，下面这些说法都是错误的：

- “用了 Docker，所以 Agent 自动变成 Ultra”；
- “选择 Pro，就会用生产模式启动服务”；
- “Local Sandbox 只能运行 Flash”；
- “启用 Sub-agent 就等于启用了 Kubernetes Provisioner”。

可以用一句话记忆：

> **服务模式决定进程怎么跑，Sandbox 模式决定动作在哪里执行，Agent 模式决定这一轮任务怎么思考和组织工作。**

---

## 5. 四类配置载体分别负责什么

### 5.1 `config.yaml`：主应用配置

`config.yaml` 位于仓库根目录，是 DeerFlow 的主配置文件。它主要描述：

- 可用模型及其 Provider；
- 工具组和工具；
- Sandbox Provider；
- Token 预算与递归上限；
- Skills 路径；
- Sub-agent 限额；
- Memory、持久化、Checkpoint、Run Event；
- Scheduler、Channel、鉴权、日志和插件等运行时能力。

它是“**这个 DeerFlow 实例拥有哪些能力、这些能力如何运行**”的主要声明。

仓库提交的是 `config.example.yaml`，实际运行使用 `config.yaml`。真实文件已在 `.gitignore` 中忽略，不应提交。

### 5.2 `extensions_config.json`：可运行时管理的扩展配置

`extensions_config.json` 主要保存：

- `mcpServers`：MCP Server 连接配置；
- `skills`：Skill 的启用状态；
- `middlewares`：固定插槽中的配置式 Middleware；
- `mcpInterceptors`：MCP 拦截器。

它与 `config.yaml` 分离，一个重要原因是：Gateway 可以通过 API 管理 MCP 和 Skill 状态，而主配置中的某些内容属于运维人员控制的启动配置。

如果文件不存在，Extensions 配置可以为空，DeerFlow 不会仅因为缺少该文件就必然失败。需要启用 MCP 时，可以从模板手工创建：

```powershell
Copy-Item .\extensions_config.example.json .\extensions_config.json
```

注意：当前 `scripts/configure.py` 所实现的 `make config` 会创建：

```text
config.yaml
.env
frontend/.env
```

它不会创建 `extensions_config.json`。这一点应以当前脚本行为为准，而不是凭文档描述猜测。

### 5.3 根目录 `.env`：后端与 Compose 的环境变量

根目录 `.env` 通常保存：

- 模型 API Key；
- 搜索、抓取、图像生成等工具的密钥；
- 数据库连接串；
- 对外监听地址和端口；
- Gateway、Provisioner、Channel 等服务所需环境变量。

`config.yaml` 中可以通过 `$变量名` 引用它们：

```yaml
models:
  - name: my-openai-model
    use: langchain_openai:ChatOpenAI
    model: gpt-4.1
    api_key: $OPENAI_API_KEY
```

对应的 `.env`：

```dotenv
OPENAI_API_KEY=替换为真实密钥
```

配置解析器看到以 `$` 开头的完整字符串时，会从进程环境中取值。主配置引用了不存在的环境变量时，加载会报错，而不是把 `$OPENAI_API_KEY` 当成真实密钥发送给 Provider。

### 5.4 `frontend/.env`：Frontend 构建与运行配置

`frontend/.env` 由 Next.js 读取，负责前端侧变量。它和根目录 `.env` 不应混为一谈：

- 根目录 `.env` 不会自动成为浏览器中的变量；
- Frontend 中只有按 Next.js 规则暴露的变量才可能进入浏览器包；
- 任何会被浏览器读取的变量都不应包含服务端秘密。

密钥应由 Gateway 使用，Frontend 通过 Gateway API 获取结果，而不是让浏览器直接持有模型密钥。

### 5.5 四类文件的关系

```text
.env ---------------------┐
                         │ 解析 $ENV_VAR
config.yaml -------------┼------> AppConfig ------> 模型 / 工具 / Sandbox / Runtime
                         │
extensions_config.json --┘                  └------> MCP / Skills / Middleware

frontend/.env ------------------------------> Next.js 构建与浏览器可见配置
```

`config.yaml` 可以显式声明 `extensions` 的部分字段。当 YAML 明确声明某个扩展字段时，该字段会覆盖 `extensions_config.json` 中对应的值。不要在两个文件里重复维护同一项，除非你清楚覆盖关系。

---

## 6. DeerFlow 如何找到配置文件

### 6.1 `config.yaml` 的查找优先级

主配置的查找顺序是：

1. 代码显式传入的 `config_path`；
2. 环境变量 `DEER_FLOW_CONFIG_PATH`；
3. `DEER_FLOW_PROJECT_ROOT` 指定项目根目录下的 `config.yaml`，或未设置项目根时从当前工作目录确定；
4. 为兼容 Monorepo 而保留的旧版 backend/仓库根目录候选位置。

找不到时会抛出 `FileNotFoundError`。

推荐做法是始终从仓库根目录启动，并把主配置放在：

```text
deer-flow/config.yaml
```

只有在部署系统明确要求配置与代码分离时，才使用 `DEER_FLOW_CONFIG_PATH`。

### 6.2 `extensions_config.json` 的查找优先级

扩展配置的顺序类似：

1. 代码传入的路径；
2. `DEER_FLOW_EXTENSIONS_CONFIG_PATH`；
3. 项目根目录下的 `extensions_config.json`；
4. 兼容旧版的 `mcp_config.json`；
5. Monorepo 的旧候选位置。

与主配置不同，扩展配置找不到时可以返回空配置，因为 MCP 和额外 Skill 是可选能力。

### 6.3 运行状态目录

`DEER_FLOW_HOME` 指定运行状态目录，默认是项目根目录下的：

```text
.deer-flow/
```

这里可能包含数据库、用户数据、运行状态和其他本地持久化内容。它不是源码目录，也不应提交到 Git。

不要把 `DEER_FLOW_HOME` 指向仓库根目录本身，更不要指向系统用户目录等宽泛位置。运行时清理、迁移和备份都应针对一个明确的专用目录。

---

## 7. 从零开始准备开发环境

### 7.1 必要依赖

本地开发主要依赖：

- Node.js 22 或更高版本；
- pnpm，项目脚本也可以通过 Corepack 解析；
- uv，用于 Python 依赖和虚拟环境；
- Nginx，用作本地统一入口；
- Git 和 Make；
- 如果选择容器 Sandbox 或 Docker 栈，还需要 Docker Desktop、Docker Engine 或受支持的容器运行时。

Windows 用户需要特别注意：当前依赖检查会提示本地 Nginx 更适合通过 WSL 使用；不想配置本机 Nginx 时，可以选择 Docker 运行方式。

### 7.2 `make check` 与 `make doctor` 不相同

`make check` 检查的是基础工具链，例如：

```text
Node.js
pnpm
uv
nginx
```

`make doctor` 检查的是已经配置后的 DeerFlow 健康状态，例如：

- 配置文件是否存在；
- 配置版本是否过期；
- 是否至少配置了一个模型；
- API Key 引用是否合理；
- Sandbox Provider 是否可用；
- 当前持久化、依赖和可选能力是否匹配。

可以记成：

> `check` 回答“机器是否具备开发条件”，`doctor` 回答“这个 DeerFlow 实例是否配置健康”。

### 7.3 推荐的首次启动顺序

对一台新机器，建议按下面顺序执行：

```powershell
make check
make config
make install
# 编辑 .env 与 config.yaml，至少配置一个真实可用模型
make doctor
make dev
```

这里有三个容易忽略的事实：

1. `make dev` 不会替你生成配置文件；
2. `make config` 检测到 `config.yaml`、`config.yml` 或 `configure.yml` 已存在时会中止，不会覆盖；
3. `make install` 会安装后端依赖、前端依赖和 pre-commit hook，可能需要较长时间和网络访问。

如果希望通过交互式向导生成一份更小、更接近可运行状态的配置，可以使用：

```powershell
make setup
```

`make setup` 与 `make config` 的定位不同：

- `make setup`：交互式选择 Provider、模型、搜索工具和 Sandbox，并生成最小配置；
- `make config`：复制完整模板，适合希望手工阅读和选择高级选项的人。

二者通常二选一，不需要连续执行。

### 7.4 配置升级

模板中的 `config_version` 用于检测本地配置是否落后。升级代码后，如果 Doctor 或日志提示版本过期，执行：

```powershell
make config-upgrade
```

升级前建议：

1. 备份本地 `config.yaml`；
2. 确认 `.env` 中仍保留所需密钥；
3. 对比新增字段的默认值；
4. 再运行 `make doctor`。

不要简单地用新模板覆盖旧配置，否则可能丢失本地模型、数据库、Sandbox 和安全策略。

---

## 8. 阅读模型配置

### 8.1 一条模型配置包含三层名字

下面是一条教学用的 OpenAI 兼容示例：

```yaml
models:
  - name: main-model
    display_name: Main Model
    use: langchain_openai:ChatOpenAI
    model: provider-model-id
    api_key: $MODEL_API_KEY
    base_url: https://provider.example.com/v1
    request_timeout: 600
    max_retries: 2
    max_tokens: 8192
    context_window: 128000
    supports_thinking: false
    supports_vision: false
    supports_reasoning_effort: false
```

不要混淆这几个字段：

| 字段 | 含义 |
| --- | --- |
| `name` | DeerFlow 内部引用这个配置项的稳定名称 |
| `display_name` | Frontend 展示给用户看的名称 |
| `use` | 要实例化的 Python 模型类，格式为 `模块:类或变量` |
| `model` | 发送给模型 Provider 的真实模型 ID |
| `base_url` | Provider 或兼容网关地址 |
| `api_key` | 凭证，推荐只引用环境变量 |

例如，Sub-agent 配置中的模型覆盖引用的是 `name`，不是 `display_name`，也不一定等于 Provider 的 `model`。

### 8.2 `use` 决定协议适配行为

“接口看起来兼容 OpenAI”不意味着所有模型都应该使用同一个类。不同 Provider 可能在以下方面有差异：

- 思考内容使用哪个响应字段；
- Tool Call 是否携带特殊签名；
- 是否允许关闭思考；
- Token 用量是否出现在流式响应中；
- 超时、重试和 `finish_reason` 是否符合标准；
- 历史推理内容是否必须回传。

因此，仓库为部分 Provider 提供了 patched model class。配置时优先复制 `config.example.yaml` 中与你的 Provider 对应的示例，不要仅因为端点兼容 OpenAI 就一律使用 `ChatOpenAI`。

### 8.3 `max_tokens` 与 `context_window` 不同

- `max_tokens`：一次模型调用允许生成的最大输出量；
- `context_window`：输入上下文与输出总和的容量。

`context_window` 还会影响前端上下文占用展示和按比例触发的自动摘要。第三方 OpenAI 兼容模型通常没有内置 Profile，如果不配置该值，系统可能无法计算百分比型摘要阈值。

这些数值必须来自 Provider 的真实文档，不能因为“越大越好”而随意填写。写得大于 Provider 实际限制，只会把错误推迟到请求阶段。

### 8.4 能力声明不是装饰字段

常见能力字段包括：

```yaml
supports_thinking: true
supports_vision: true
supports_reasoning_effort: true
```

它们会影响界面选项和模型构造逻辑：

- `supports_thinking` 为 false 时，思考相关配置不能正常启用；
- `supports_vision` 决定该模型能否处理图像类输入或配合相关能力；
- `supports_reasoning_effort` 决定是否向 Provider 传递推理深度。

能力声明必须与模型实际能力和 `when_thinking_enabled` / `when_thinking_disabled` 配置一致。声明为 true 不会凭空让 Provider 获得能力。

### 8.5 默认模型

当请求没有指定模型时，系统通常会使用配置列表中的默认选择逻辑；很多路径最终落到第一个可用模型。为了让实验可复现：

- 第一次只配置一个模型；或
- 在 Frontend 明确选择模型，并把选择记录进实验表。

否则，模式对比可能混入“模型不同”这个额外变量。

---

## 9. 阅读工具组与工具配置

### 9.1 工具组是分类和策略单元

默认模板中的工具组类似：

```yaml
tool_groups:
  - name: web
  - name: file:read
  - name: file:write
  - name: bash
  - name: browser
  - name: knowledge
```

工具组本身不是可执行工具。它用于组织、授权和筛选工具能力。

### 9.2 工具项才是可调用能力

```yaml
tools:
  - name: read_file
    group: file:read
    use: deerflow.sandbox.tools:read_file_tool

  - name: write_file
    group: file:write
    use: deerflow.sandbox.tools:write_file_tool

  - name: web_search
    group: web
    use: deerflow.community.ddg_search.tools:web_search_tool
```

每个工具至少回答三个问题：

1. Agent 看到的工具名是什么；
2. 工具属于哪个组；
3. 实际实现从哪个 Python 路径加载。

工具还可以携带 Provider Key、超时、结果上限、代理地址等专属配置。

### 9.3 “配置了工具”不等于“工具一定能成功执行”

工具可用需要同时满足多层条件：

```text
工具出现在 config.yaml
        ↓
工具实现可以导入
        ↓
依赖包已安装
        ↓
所需密钥存在
        ↓
当前 Agent / 用户有权限
        ↓
Sandbox 或外部网络允许执行
        ↓
模型正确地产生 Tool Call
```

因此，页面看到工具或模型知道工具名，只能证明“工具 Schema 进入了能力集合”，不能证明最后一步执行必然成功。

### 9.4 工具组不会自动随 Flash、Pro 切换

四种 Agent 模式主要切换思考、规划和 Sub-agent。它们不是四套静态工具白名单。普通工具是否存在，仍然主要由 `config.yaml`、Agent 配置、授权和运行时筛选决定。

Ultra 的特殊之处是启用 Sub-agent 后，会向 Lead Agent 增加委派相关工具，例如 `task` 和状态查询能力。具体集合还会受到允许的 Sub-agent 类型和授权约束。

---

## 10. 阅读 Sandbox 配置

### 10.1 Local Sandbox

模板默认配置类似：

```yaml
sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
  allow_host_bash: false
  bash_output_max_chars: 20000
  read_file_output_max_chars: 50000
  ls_output_max_chars: 20000
  bash_command_timeout: 600
```

Local Sandbox 适合：

- 本地单用户学习；
- 主要测试文件读取、写入和 Artifact；
- 不需要执行不可信 Shell 命令的场景。

它不是强安全隔离边界。`allow_host_bash: false` 默认禁止宿主侧 Bash，是一个重要的安全默认值。只有在完全信任输入、明确理解风险的单用户环境中，才考虑开启。

### 10.2 AIO 容器 Sandbox

简化示例：

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  image: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0
  replicas: 3
  idle_timeout: 600
  network:
    mode: allowlist
    allow_domains:
      - pypi.org
      - files.pythonhosted.org
      - registry.npmjs.org
      - github.com
    approval: prompt
```

AIO Sandbox 把命令和文件操作放入独立容器，隔离性强于 Local Provider，但仍要理解：

- Gateway 需要管理 Sandbox 容器；
- 镜像版本必须满足 DeerFlow 使用的 Sandbox HTTP API；
- 挂载目录会进入信任边界；
- 网络开放程度会影响任务可完成性和安全性；
- Docker-in-Docker 与挂载宿主 Docker Socket 是完全不同的安全方案。

### 10.3 DooD 与 Docker Socket 风险

本地 AIO 模式可能通过宿主 Docker Daemon 创建容器。把 `/var/run/docker.sock` 挂载给 Gateway，相当于赋予 Gateway 对宿主 Docker 的高权限控制。

这不是普通文件挂载。能够访问 Docker Socket 的进程通常可以创建挂载宿主文件系统的新容器，实际风险接近宿主 root 权限。

因此：

- 默认 Compose 不挂载 Docker Socket；
- AIO/DooD 模式通过专用 overlay 显式启用；
- 对公网或多租户部署，优先考虑 Provisioner/Kubernetes 等更清晰的隔离方案；
- 即使是容器 Sandbox，也必须限制挂载、网络和凭证。

### 10.4 Provisioner 模式

```yaml
sandbox:
  use: deerflow.community.aio_sandbox:AioSandboxProvider
  provisioner_url: http://provisioner:8002
  provisioner_api_key: $PROVISIONER_API_KEY
```

Provisioner 作为资源控制面，通过 Kubernetes API 创建 Sandbox Pod。此时：

- Gateway 不需要直接持有宿主 Docker Socket；
- Sandbox 镜像通常由 Provisioner 环境变量配置，而不是只看 `sandbox.image`；
- Gateway 与 Provisioner 的 API Key 必须匹配；
- 网络、存储卷和命名空间策略成为部署安全的一部分。

### 10.5 本地地址在容器中的含义

这是最常见的配置错误之一：

```text
127.0.0.1 / localhost 永远指向“当前进程所在的网络命名空间”。
```

如果 Gateway 在容器中：

- `http://localhost:11434` 指向 Gateway 容器本身；
- 若模型服务运行在宿主机，Docker Desktop 常使用 `host.docker.internal`；
- 若服务是同一个 Compose 栈中的另一个 Service，应使用 Compose Service 名，例如 `http://gateway:8001`。

所以“浏览器能访问某地址”不代表 Gateway 容器也能访问它。

---

## 11. 四种 Agent 工作模式的真实区别

### 11.1 Frontend 的映射逻辑

当前 Frontend 把模式转换为请求上下文：

| 模式 | `thinking_enabled` | `is_plan_mode` | `subagent_enabled` | 默认 `reasoning_effort` |
| --- | ---: | ---: | ---: | --- |
| `flash` | false | false | false | 不设置 |
| `thinking` | true | false | false | low |
| `pro` | true | true | false | medium |
| `ultra` | true | true | true | high |

如果用户显式选择了推理深度，则显式值优先于表中的默认值。Provider 是否真正接受该值，还取决于模型配置中的能力声明和适配类。

这张表是理解四种模式的核心证据。

### 11.2 Flash：不启用思考、规划和委派

Flash 适合：

- 简短问答；
- 明确的一步操作；
- 低风险格式转换；
- 需要低延迟、低成本的任务。

Flash 并不等于“不能使用工具”。模型仍然可以调用已提供的普通工具。它只是不会通过模式开关启用思考、Todo 规划和 Sub-agent。

预期特征：

- 首个 Token 更快；
- 模型调用次数通常更少；
- 复杂任务中更容易遗漏步骤或缺少验证；
- 不会产生 Sub-agent 委派。

### 11.3 Thinking：思考后行动，但不启用计划中间件

Thinking 适合：

- 有一定推理要求但任务规模不大；
- 需要在速度和准确性之间平衡；
- 不需要显式拆分长期步骤的任务。

预期特征：

- `thinking_enabled=true`；
- 默认推理深度为 low；
- 不启用 Todo 规划；
- 不启用 Sub-agent。

### 11.4 Pro：思考 + 规划

Pro 在 Thinking 的基础上启用计划模式：

```text
thinking_enabled = true
is_plan_mode = true
subagent_enabled = false
```

Lead Agent 会得到 Todo/计划相关的运行时支持，更适合：

- 多步骤代码修改；
- 需要先调查、再实施、再验证的任务；
- 有多个验收条件，但不适合或不允许并行委派的任务。

Pro 不等于一定产生更多工具调用。对于简单任务，Agent 仍可能很快完成。模式提供能力，不强制模型机械地使用每一种能力。

### 11.5 Ultra：思考 + 规划 + Sub-agent

Ultra 在 Pro 基础上启用 Sub-agent：

```text
thinking_enabled = true
is_plan_mode = true
subagent_enabled = true
```

它适合：

- 可以拆成多个相对独立子任务的复杂工作；
- 需要并行调研多个模块；
- 需要不同专业角色分别分析、实现或验证；
- 任务收益足以覆盖额外模型调用和上下文开销。

但 `subagent_enabled=true` 不保证一定启动 Sub-agent。还必须满足：

- 当前 Agent 允许至少一种 Sub-agent；
- 委派工具成功进入工具集合；
- 任务确实适合委派；
- Lead Agent 决定调用委派工具；
- Sub-agent 运行时未超出并发、队列、次数、Token 或超时限制。

因此，实验中“Ultra 没有调用 Sub-agent”不一定是 Bug，可能是模型判断任务无需委派，也可能是配置或权限禁用了委派。需要结合工具列表和事件证据判断。

### 11.6 模式不是质量等级保证

Ultra 能力最多，但不表示任何任务都应该使用 Ultra：

- 简单任务使用 Ultra 可能增加延迟和费用；
- 错误拆分会增加沟通和汇总损耗；
- 多个 Sub-agent 可能重复搜索；
- 更多模型调用意味着更大的失败面；
- 结果质量最终仍需测试、Schema、事实核验或人工验收。

合理选择模式的原则是：

> **任务需要多少组织能力，就启用多少能力；不要把模式名称当成答案质量承诺。**

---

## 12. Sub-agent 配置如何约束 Ultra

Ultra 只是打开入口，真正的资源边界由后端配置控制。

### 12.1 进程级执行容量

模板中的关键配置：

```yaml
subagent_runtime:
  max_running: 3
  max_queued: 64
  admission_policy: queue
  queue_timeout_seconds: 300
```

它们分别限制：

- 同时实际运行多少个 Sub-agent；
- 最多排队多少个任务；
- 满载时排队还是拒绝；
- 排队等待多久后超时。

这部分在 Gateway 生命周期启动时构造，修改后需要重启。

### 12.2 单次 Lead Run 的委派上限

```yaml
subagents:
  max_total_per_run: 6
```

它限制一个 Lead Agent Run 最多启动多少次委派，防止模型反复拆分、无限消耗资源。

还可以设置：

- 全局或单类 Sub-agent 超时；
- 最大 Turn 数；
- Token Budget；
- 专用模型；
- 工具白名单；
- Skill 白名单；
- Custom Sub-agent 的系统提示词。

实验报告中应记录这些上限。否则，两个环境中的 Ultra 即使名称相同，也可能拥有完全不同的并行能力。

---

## 13. 环境变量与密钥管理

### 13.1 为什么不把密钥直接写进 YAML

错误示例：

```yaml
api_key: sk-real-secret-value
```

推荐示例：

```yaml
api_key: $OPENAI_API_KEY
```

这样做的收益包括：

- 配置结构可以共享，秘密不进入文件内容；
- 不同环境可以复用相同 YAML；
- 密钥轮换不需要修改业务配置；
- 容器、CI 和 Secret Manager 更容易注入；
- 降低误提交和日志泄漏风险。

### 13.2 `.gitignore` 不是完整的安全机制

当前仓库会忽略：

```text
.env
config.yaml
extensions_config.json
.deer-flow/
```

但 `.gitignore` 只能阻止“尚未被跟踪”的文件被普通 `git add` 加入。它不能：

- 从历史中删除已经提交过的密钥；
- 阻止截图、日志或终端输出泄密；
- 阻止使用 `git add -f` 强制添加；
- 保证 Artifact、Support Bundle 或错误堆栈一定完成脱敏。

提交前至少执行：

```powershell
git status --short
git check-ignore -v .env config.yaml extensions_config.json
```

如果密钥曾进入 Commit，应立即撤销或轮换密钥，然后再处理 Git 历史。仅删除当前文件不够。

### 13.3 最小权限与分环境

建议：

- 开发、测试、生产使用不同密钥；
- 搜索、GitHub、数据库等 Token 只授予必要权限；
- 只读任务使用只读 Token；
- 设置额度、速率限制和过期时间；
- 不把个人长期 CLI 凭证整体挂载进不可信容器；
- 生产环境优先使用平台 Secret，而不是把 `.env` 复制进镜像。

### 13.4 环境变量在哪个进程中可见

调试密钥问题时，必须问：**谁需要这个变量？**

| 使用者 | 变量必须进入哪里 |
| --- | --- |
| Gateway 中的模型 Provider | Gateway 进程或 Gateway 容器 |
| Gateway 中的 Web Tool | Gateway 进程或 Gateway 容器 |
| stdio MCP Server | 启动 MCP 子进程时的 `env` |
| Sandbox 内执行的脚本 | Sandbox 的 `environment` 或单次受控注入 |
| Frontend 构建 | Frontend 构建/运行进程，但绝不能暴露后端密钥 |

“宿主机 `echo $env:OPENAI_API_KEY` 有值”不能证明 Docker 内的 Gateway 能看到它。

还要注意，环境变量属于进程环境，不是普通的每请求配置。修改 `.env` 后，生产模式下应重启读取它的服务；开发启动器虽然会监听 `.env` 并可能触发 Gateway 重载，但验证时仍应确认进程确实重新启动，并创建一个新 Run。不要把 `config.yaml` 的按请求重读机制套用到环境变量上。

---

## 14. 哪些配置热加载，哪些需要重启

### 14.1 为什么存在两种生效边界

Gateway 会在请求阶段重新读取一部分 `AppConfig`，所以模型、工具和一些每 Run 参数可以在下一次请求生效。

但某些基础设施对象在 Gateway 启动时只构造一次，例如：

- 数据库 Engine；
- Checkpointer；
- Run Event Store；
- Stream Bridge；
- Sandbox Provider 单例；
- Scheduler 后台任务；
- Channel 客户端；
- Sub-agent Admission Controller。

只重新读取 YAML，无法安全地替换这些已经在运行的对象，因此必须重启。

### 14.2 常见重启项

当前重启边界至少包括：

- `plugins`；
- `database`；
- `checkpointer`；
- `run_events`；
- `agent_storage`；
- `stream_bridge`；
- `sandbox`；
- `skills.container_path`；
- `log_level` 与 `logging`；
- `channels` 与 `channel_connections`；
- `scheduler` 的后台轮询字段；
- `mcp_tasks`；
- `subagent_runtime`；
- `subagent_batches`；
- `run_ownership`；
- `dedupe_storage`。

修改这些配置后，至少重启 Gateway。多实例部署中，涉及数据库、所有权和 Scheduler 的变更通常需要协调重启全部 Gateway 实例，不能只重启一台。

### 14.3 常见按请求生效项

例如：

- Gateway Run 的 `recursion_limit` 默认值；
- `max_recursion_limit` 的请求侧限制；
- 部分模型、工具和每 Run Agent 选项；
- Scheduler 的 `recursion_limit`，它在派发时读取。

但不能只凭“字段看起来像业务配置”判断。最可靠的证据是：

1. 查看 `deerflow/config/reload_boundary.py` 的注册表；
2. 查看字段是否在 Gateway lifespan 中被捕获；
3. 修改后观察新请求的实际组装日志；
4. 不确定时重启，并在实验记录中注明。

### 14.4 开发热更新不等于配置一定热生效

`make dev` 会启用代码热重载，但仍需区分：

- 文件变化触发 Uvicorn 重启：相当于进程重建；
- 应用每次请求重新读取配置：无需进程重启；
- Frontend 本地状态：可能需要刷新页面或重新创建 Run；
- 已经运行中的 Agent：通常不会中途改变本轮配置快照。

所以，验证配置变化时应创建一个**新的 Run**，而不是观察已经启动的 Run。

---

## 15. 四种服务启动方式

### 15.1 `make dev`

`make dev` 会：

- 先执行依赖检查；
- 启动 Gateway，默认端口 8001；
- 启动 Frontend，固定使用 3000；
- 启动 Nginx，统一入口 2026；
- 为 Gateway 和 Frontend 启用开发态重载。

浏览器应访问：

```text
http://localhost:2026
```

不要把 3000 当成完整系统入口。直接访问 Frontend 可能绕开 Nginx 的 API 路由规则。

### 15.2 `make start`

`make start` 使用本地生产方式运行：

- Gateway 不启用热更新；
- Frontend 使用生产构建；
- 仍由本地 Nginx 提供 2026 统一入口。

如果已经存在可复用的 Frontend 构建，可以显式使用：

```powershell
make start SKIP_FRONTEND_BUILD=1
```

若没有现成构建，该选项会失败，而不是偷偷退回开发服务器。

### 15.3 `make docker-start`

它启动 Docker 开发环境，并根据 `config.yaml` 中的 Sandbox 配置选择相应 Compose overlay。适合：

- 本机依赖较难统一；
- Windows 不想单独安装 Nginx；
- 需要测试容器网络和挂载；
- 需要更接近部署环境的开发方式。

### 15.4 `make up`

它构建并启动生产 Docker 栈。默认只把 Nginx 入口发布到宿主机：

```text
127.0.0.1:2026
```

Gateway 的 `0.0.0.0:8001` 和 Frontend 的容器内端口用于容器网络，并不等于它们直接暴露在公网。

如果设置：

```dotenv
BIND_HOST=0.0.0.0
```

则 Nginx 会监听所有宿主机接口。只有在已经配置 TLS、鉴权、防火墙和可信反向代理时才应这样做。

### 15.5 健康检查的层次

至少区分：

```text
端口可连接
  ≠ HTTP 健康接口成功
  ≠ Gateway 已完成启动
  ≠ 模型凭证有效
  ≠ Agent 能调用工具
  ≠ 用户任务最终正确
```

Docker 生产栈使用 Gateway readiness 检查。它能证明 Gateway 到达就绪状态，但不能替代一次真实模型请求和工具调用测试。

---

## 16. 实践一：建立环境与配置证据表

### 任务目标

不要直接修改配置。先用只读命令建立当前环境快照。

### 建议命令

```powershell
node --version
pnpm --version
uv --version
docker version
make check
```

确认配置文件是否存在，但不要把内容中的密钥打印到共享终端：

```powershell
Test-Path .\config.yaml
Test-Path .\.env
Test-Path .\frontend\.env
Test-Path .\extensions_config.json
```

确认敏感文件被忽略：

```powershell
git check-ignore -v .env config.yaml extensions_config.json
git status --short
```

### 产出模板

| 检查项 | 结果 | 证据 | 是否阻塞 |
| --- | --- | --- | --- |
| Node.js ≥ 22 |  | `node --version` |  |
| pnpm 可用 |  | `pnpm --version` |  |
| uv 可用 |  | `uv --version` |  |
| Nginx 或 Docker 可用 |  | 命令输出 |  |
| `config.yaml` 存在 |  | `Test-Path` |  |
| 至少一个模型 |  | 脱敏后的模型名 |  |
| Sandbox 明确 |  | `sandbox.use` |  |
| 敏感文件被忽略 |  | `git check-ignore` |  |

禁止把完整 `.env`、完整请求 Header、API Key 或数据库密码粘贴到学习文档。

---

## 17. 实践二：构造最小可运行配置

### 17.1 任务目标

从完整模板中识别“启动所必需的最小集合”，而不是启用所有功能。

教学示例：

```yaml
config_version: 43

models:
  - name: lesson-model
    display_name: Lesson Model
    use: langchain_openai:ChatOpenAI
    model: provider-model-id
    api_key: $LESSON_MODEL_API_KEY
    base_url: https://provider.example.com/v1
    request_timeout: 600
    max_retries: 2
    context_window: 128000
    supports_thinking: false
    supports_vision: false
    supports_reasoning_effort: false

tool_groups:
  - name: file:read
  - name: file:write

tools:
  - name: ls
    group: file:read
    use: deerflow.sandbox.tools:ls_tool
  - name: read_file
    group: file:read
    use: deerflow.sandbox.tools:read_file_tool
  - name: write_file
    group: file:write
    use: deerflow.sandbox.tools:write_file_tool

sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
  allow_host_bash: false
```

这只是结构示例，不代表 `provider-model-id`、地址和能力声明适用于你的 Provider。实际配置必须从当前模板中选择对应 Provider 段落。

### 17.2 验证步骤

1. 在 `.env` 中设置教学用密钥；
2. 在 `config.yaml` 中只写 `$LESSON_MODEL_API_KEY` 引用；
3. 运行 `make doctor`；
4. 修复所有阻塞项；
5. 运行 `make dev`；
6. 访问 `http://localhost:2026`；
7. 创建新会话，发送一个不需要工具的短问题；
8. 再发送一个只需读取文件的任务；
9. 保存脱敏日志和结果截图。

### 17.3 通过标准

- Doctor 没有阻塞性错误；
- 页面可以加载；
- Gateway 能完成真实模型调用；
- Agent 可以读取允许范围内的文件；
- 未启用 `allow_host_bash` 时，不能把 Local Provider 当成任意宿主命令执行器；
- Git 状态中不出现敏感配置文件。

---

## 18. 实践三：用四种 Agent 模式执行同一个任务

### 18.1 为什么必须使用同一个任务

只有保持以下变量不变，模式对比才有意义：

- 同一个模型；
- 同一个 Prompt；
- 同一个代码版本；
- 同一个工具配置；
- 同一个 Sandbox；
- 同一个推理深度规则；
- 尽量相近的初始上下文。

建议每种模式创建一个新 Thread，避免前一次对话历史影响后一次结果。

### 18.2 推荐实验任务

选择一个具有多步骤、可验证产出、但风险较低的任务：

> 阅读 `learn/README.md`，统计六个阶段分别包含多少节课，检查总数是否为 30；把阶段名称、课程范围、课程数量和核验结论写入 `learn/outputs/lesson-03-course-audit.md`。写完后重新读取文件，确认表格数字与 README 一致。

这个任务同时包含：

- 文件读取；
- 信息提取；
- 计数与交叉校验；
- Markdown 文件写入；
- 结果复读验证；
- 可以规划，但规模又不会大到难以观察。

为了避免四次运行互相覆盖，可以分别要求输出：

```text
lesson-03-course-audit-flash.md
lesson-03-course-audit-thinking.md
lesson-03-course-audit-pro.md
lesson-03-course-audit-ultra.md
```

除了目标文件名，其他 Prompt 内容应保持一致。

### 18.3 需要记录的指标

| 指标 | 记录方法 |
| --- | --- |
| 模式 | Frontend 当前选择 |
| 模型 | 模型配置 `name` 与 Provider 模型 ID |
| 总耗时 | 从发送到 Run 结束 |
| 模型调用次数 | Trace、Run Event 或可观测平台 |
| 输入/输出 Token | Workspace 用量信息或 Provider Usage |
| 普通工具调用次数 | 统计 `read_file`、`write_file` 等事件 |
| 是否创建计划 | 观察 Todo/计划事件 |
| Sub-agent 数量 | 统计委派事件，不能凭回答措辞猜测 |
| 是否生成文件 | 检查目标路径 |
| 是否复读验证 | 检查工具调用顺序 |
| 内容是否正确 | 对照 README 人工或脚本核验 |
| 错误与重试 | 记录错误类型和重试次数 |

### 18.4 模式对比表模板

| 维度 | Flash | Thinking | Pro | Ultra |
| --- | --- | --- | --- | --- |
| `thinking_enabled` | false | true | true | true |
| `is_plan_mode` | false | false | true | true |
| `subagent_enabled` | false | false | false | true |
| 默认推理深度 | 无 | low | medium | high |
| 实际模型调用次数 |  |  |  |  |
| 实际工具调用次数 |  |  |  |  |
| 实际 Sub-agent 数量 | 0 | 0 | 0 |  |
| 总耗时 |  |  |  |  |
| Token 总量 |  |  |  |  |
| 是否先规划 |  |  |  |  |
| 是否复读文件 |  |  |  |  |
| 结果正确性 |  |  |  |  |
| 主要失败点 |  |  |  |  |

### 18.5 如何解释实验结果

不要预设结果一定满足：

```text
Ultra > Pro > Thinking > Flash
```

合理结论可能是：

- 四种模式都正确，但 Flash 成本最低；
- Flash 漏掉复读验证，Thinking 修复了这一点；
- Pro 创建了计划，但对这个小任务收益不明显；
- Ultra 没有委派，因为任务不值得拆分；
- Ultra 委派后反而变慢，但产出证据更完整；
- 某模型不支持真实推理深度，因此四模式差异主要来自计划和委派；
- Token Usage 缺失，是 Provider 流式 Usage 没有返回，而不是调用次数为零。

实验的目标是测量当前系统行为，不是证明高级模式一定更好。

---

## 19. 实践四：验证配置生效边界

### 19.1 热加载实验

选择一个安全、容易观察的每请求字段，例如在允许范围内调整 `recursion_limit`：

1. 记录原值；
2. 完成一个新 Run；
3. 修改配置；
4. 不手工重启，创建另一个新 Run；
5. 从日志或请求行为确认新值是否生效；
6. 恢复原值。

不要把极低递归限制用于正在处理重要工作的 Thread，以免强制中断任务。

### 19.2 重启项实验

不要为了练习随意切换生产数据库。可以只做代码阅读：

1. 打开 `reload_boundary.py`；
2. 找到 `sandbox`、`database` 和 `subagent_runtime`；
3. 写出各自为什么需要重启；
4. 从调用链中找到它们在 Gateway lifespan 中被创建的位置。

### 19.3 产出模板

| 配置项 | 修改前 | 修改后 | 是否新 Run 生效 | 是否需重启 | 代码证据 |
| --- | --- | --- | --- | --- | --- |
| `recursion_limit` |  |  |  | 否 |  |
| `sandbox.use` |  |  | 不直接实验 | 是 |  |
| `database.backend` |  |  | 不直接实验 | 是 |  |
| `subagent_runtime.max_running` |  |  | 不直接实验 | 是 |  |

---

## 20. 常见故障与诊断路径

### 20.1 `config.yaml` 不存在

现象：Gateway 启动时报告找不到配置文件。

检查：

```powershell
Get-Location
Test-Path .\config.yaml
$env:DEER_FLOW_PROJECT_ROOT
$env:DEER_FLOW_CONFIG_PATH
```

处理：从仓库根目录运行 `make setup` 或 `make config`。如果使用自定义路径，确认该路径在 Gateway 所在环境中真实存在。

### 20.2 配置版本过期

现象：日志提示本地 `config_version` 低于模板版本。

处理：

```powershell
make config-upgrade
make doctor
```

升级后检查本地自定义字段是否仍保留。

### 20.3 没有模型

现象：Doctor 提示没有模型，或 Frontend 无可选模型。

检查 `models:` 是否为空、是否只保留了注释。YAML 中只有注释的 `models:` 会被解析为 `null`，最终回退为空列表，不会变成一个可用模型。

### 20.4 环境变量未定义

现象：加载主配置时报 `Environment variable ... not found`。

检查：

- `.env` 是否在 Gateway 的工作目录加载；
- 环境变量名是否完全一致；
- Docker Compose 是否把变量传给 Gateway；
- YAML 中是否写成完整的 `$NAME` 字符串；
- 变量是否只在当前 PowerShell 会话中设置，却从另一个终端启动服务。

不要通过打印完整变量值验证密钥。可以只检查是否存在和长度是否大于零。

### 20.5 API Key 无效

现象：配置成功加载，但真实模型请求返回 401 或 403。

这说明“变量存在”，但不能证明凭证有效。继续检查：

- Key 是否属于正确 Provider；
- `base_url` 是否匹配 Key 类型；
- 模型是否对账号开放；
- Key 是否过期、被撤销或权限不足；
- 请求是否经过错误的代理或兼容网关。

### 20.6 页面加载，但发送消息 502

这通常意味着 Nginx 和 Frontend 正常，但 Gateway 不可达或未就绪。检查：

```text
Nginx 日志
Gateway 进程
8001 端口
Gateway readiness
配置解析错误
数据库迁移或连接错误
```

### 20.7 模型能回答，但工具失败

按顺序检查：

1. 工具是否出现在 `tools:`；
2. `use` 路径能否导入；
3. 可选依赖是否安装；
4. 工具组和授权是否允许；
5. Sandbox Provider 是否支持该动作；
6. 路径是否位于线程工作区或显式挂载范围；
7. 容器网络是否允许外部访问。

### 20.8 Ultra 没有 Sub-agent

检查证据：

- 请求中的 `subagent_enabled` 是否为 true；
- 当前 Agent 的 `allowed_subagents` 是否为空；
- 委派工具是否出现在 Agent 工具集合；
- `subagent_runtime` 是否满载或拒绝；
- 是否达到 `max_total_per_run`；
- 任务是否足够复杂；
- 是否真的没有委派事件，而不是 UI 折叠了展示。

### 20.9 Docker 中连接本机模型失败

如果宿主机模型地址写成 `localhost`，容器会连接自己。改用：

- 同一 Compose 网络中的 Service 名；或
- Docker Desktop 支持的 `host.docker.internal`；或
- 明确配置的宿主网关地址。

同时确认模型服务监听的不是仅宿主回环且拒绝容器访问的地址。

### 20.10 修改配置后行为没变化

检查：

- 改的是不是实际被读取的配置文件；
- `DEER_FLOW_CONFIG_PATH` 是否指向另一份文件；
- 字段是否属于 restart-required；
- 是否创建了新 Run；
- Frontend 是否仍保留旧模式或旧模型偏好；
- 多 Gateway 实例是否只重启了其中一个。

---

## 21. 常见误区

### 误区一：`make config` 会生成所有配置文件

当前脚本会生成主配置和两个 `.env` 文件，不会自动生成 `extensions_config.json`。扩展配置是可选的，需要时从模板复制或通过 Gateway 管理。

### 误区二：配置中写了 API Key 引用，就说明服务能访问 Key

引用只定义取值方式。真正的变量必须进入读取配置的进程或容器。

### 误区三：`supports_thinking: true` 会让任何模型拥有思考能力

该字段是能力声明，不是能力实现。Provider、模型 ID、适配类和开关参数必须同时匹配。

### 误区四：Flash 模式不能调用工具

Flash 关闭的是模式级思考、规划和委派，不是所有普通工具。

### 误区五：Ultra 一定会创建多个 Sub-agent

Ultra 允许委派，但是否委派仍是 Agent 决策，并受权限与资源上限约束。

### 误区六：Local Sandbox 等于安全沙箱

Local Provider 更像统一文件与命令接口，不是针对不可信代码的强隔离边界。

### 误区七：Docker 内的 `localhost` 指向宿主机

它指向当前容器。跨容器或访问宿主服务必须使用正确网络地址。

### 误区八：`make dev` 的热更新意味着所有配置都无需重启

启动期构造的基础设施对象仍然需要进程重启。

### 误区九：`.gitignore` 能解决所有密钥泄漏问题

它只能降低误提交概率，不能撤销已经泄漏的凭证，也不能保护日志、截图和构建产物。

### 误区十：服务健康就代表任务正确

健康检查只覆盖部分运行条件。最终任务仍需事实核验、测试和产物检查。

---

## 22. 课后自测

### 问题 1

`config.yaml` 和 `extensions_config.json` 为什么要分开？

<details>
<summary>参考答案</summary>

`config.yaml` 是主应用和基础设施配置，包含模型、工具、Sandbox、数据库和启动期插件等；`extensions_config.json` 主要保存可由 Gateway 运行时管理的 MCP、Skill 和配置式扩展状态。分开后，API 可修改的扩展状态不会与运维控制、可能触发代码加载的主配置完全混在一起。

</details>

### 问题 2

当前四种 Agent 模式分别映射到哪些开关？

<details>
<summary>参考答案</summary>

- Flash：不思考、不规划、不委派；
- Thinking：思考，不规划，不委派；
- Pro：思考并规划，不委派；
- Ultra：思考、规划并允许委派。

默认推理深度依次为未设置、low、medium、high，但显式选择和模型能力可能改变最终传给 Provider 的值。

</details>

### 问题 3

为什么课程大纲中的 `standard` 不应用于当前实验？

<details>
<summary>参考答案</summary>

当前 Frontend 类型、用户偏好 API 和请求映射只接受 `flash`、`thinking`、`pro`、`ultra`。`standard` 是旧名称或大纲偏差，提交后会被类型或 API 校验拒绝。

</details>

### 问题 4

为什么 `name`、`use` 和 `model` 不能互换？

<details>
<summary>参考答案</summary>

`name` 是 DeerFlow 内部引用的配置名，`use` 是 Python 适配类路径，`model` 是发送给 Provider 的模型 ID。它们分别解决配置索引、协议实现和远端资源定位问题。

</details>

### 问题 5

为什么修改 `sandbox.use` 后需要重启 Gateway？

<details>
<summary>参考答案</summary>

Sandbox Provider 会被创建并缓存为运行时单例。只重新读取 YAML 不会安全地替换已经持有连接、容器和资源状态的 Provider，因此该字段属于启动期配置。

</details>

### 问题 6

宿主机 `.env` 中有模型 Key，但 Docker Gateway 返回缺少 Key，最可能的问题是什么？

<details>
<summary>参考答案</summary>

变量只存在于宿主环境，没有通过 Compose 或容器环境进入 Gateway；也可能 Gateway 读取的是另一份配置或 `.env`。应确认变量对目标容器可见，而不是只在宿主终端检查。

</details>

### 问题 7

为什么不能根据最终回答中出现“我委派了任务”就认定 Ultra 创建了 Sub-agent？

<details>
<summary>参考答案</summary>

模型文本是不可靠陈述。必须检查真实委派工具调用、Sub-agent 事件、状态记录或 Trace。回答中的措辞不能替代运行证据。

</details>

### 问题 8

`max_tokens` 与 `context_window` 有什么区别？

<details>
<summary>参考答案</summary>

前者限制单次调用的输出量，后者表示输入加输出的总上下文容量，并参与上下文占用显示和部分摘要阈值计算。

</details>

### 问题 9

为什么用四种模式做实验时应该创建四个新 Thread？

<details>
<summary>参考答案</summary>

复用历史 Thread 会让后运行模式看到前面模式的消息、工具结果和可能的摘要，破坏控制变量。新 Thread 能减少上下文差异。

</details>

### 问题 10

什么情况下简单任务更适合 Flash，而不是 Ultra？

<details>
<summary>参考答案</summary>

任务目标明确、步骤少、风险低、无需并行调查，且可用确定性校验快速验证时，Ultra 的计划和委派开销可能没有收益。此时 Flash 通常延迟更低、成本更小。

</details>

---

## 23. 本课产出模板

将下面内容保存为：

```text
learn/outputs/lesson-03-runtime-mode-comparison.md
```

```markdown
# DeerFlow 环境与运行模式实验报告

## 1. 实验环境

- 操作系统：
- DeerFlow Commit：
- Node.js：
- pnpm：
- uv：
- 启动方式：make dev / make start / make docker-start / make up
- Gateway 地址：
- 模型配置名：
- Provider 模型 ID：
- Sandbox Provider：
- config_version：

## 2. 配置安全检查

- [ ] `.env` 被 Git 忽略
- [ ] `config.yaml` 被 Git 忽略
- [ ] `extensions_config.json` 被 Git 忽略
- [ ] 文档和截图不包含完整密钥
- [ ] 未向 Frontend 暴露服务端密钥

## 3. 配置职责说明

### config.yaml

### extensions_config.json

### 根目录 .env

### frontend/.env

## 4. 三类模式

### 服务启动模式

### Sandbox 模式

### Agent 工作模式

## 5. 对照实验任务

完整 Prompt：

控制变量：

正确性判定规则：

## 6. 四模式结果

| 维度 | Flash | Thinking | Pro | Ultra |
| --- | --- | --- | --- | --- |
| thinking_enabled | false | true | true | true |
| is_plan_mode | false | false | true | true |
| subagent_enabled | false | false | false | true |
| 模型调用次数 | | | | |
| 工具调用次数 | | | | |
| Sub-agent 数量 | 0 | 0 | 0 | |
| 总耗时 | | | | |
| Token 总量 | | | | |
| 是否规划 | | | | |
| 是否复读验证 | | | | |
| 结果是否正确 | | | | |

## 7. 证据

- Run / Thread ID：
- 脱敏日志：
- 输出文件：
- 截图：

## 8. 结论

### 最适合本任务的模式

### 原因

### 实验局限

### 如果扩大任务规模，预期会发生什么变化
```

---

## 24. 本课验收标准

### 基础通过

- [ ] 能说明四类配置文件的职责；
- [ ] 能正确说出当前四种 Agent 模式名称；
- [ ] 能解释三类“模式”的区别；
- [ ] 至少配置一个真实可用模型；
- [ ] `make doctor` 没有阻塞项；
- [ ] 能通过 2026 统一入口完成一次模型请求；
- [ ] 确认敏感配置文件未被 Git 跟踪。

### 实践通过

- [ ] 使用同一模型和同一任务测试四种模式；
- [ ] 每种模式使用独立 Thread；
- [ ] 记录模型调用、工具调用、耗时和 Token；
- [ ] 用事件证据判断是否规划和委派；
- [ ] 对输出文件做独立正确性检查；
- [ ] 完成运行模式对比表。

### 深入通过

- [ ] 能解释模型 `use` 与 `model` 的差别；
- [ ] 能说明 Ultra 为什么不保证启动 Sub-agent；
- [ ] 能从代码找到模式映射逻辑；
- [ ] 能从重启边界注册表判断配置何时生效；
- [ ] 能解释 Docker 中 `localhost` 的真实含义；
- [ ] 能说出 Docker Socket 对安全边界的影响；
- [ ] 能对一次启动失败给出分层证据链。

---

## 25. 建议保存的学习证据

建议在 `learn/outputs/` 下保存：

```text
lesson-03-environment-check.md
lesson-03-runtime-mode-comparison.md
lesson-03-course-audit-flash.md
lesson-03-course-audit-thinking.md
lesson-03-course-audit-pro.md
lesson-03-course-audit-ultra.md
```

还可以保存：

- `make doctor` 的脱敏输出；
- 四个 Run 的 ID；
- 工具调用时间线截图；
- Token Usage 截图；
- 配置生效边界证据表；
- 一次失败配置和修复后的对照日志。

不要保存：

- 完整 `.env`；
- API Key；
- Session Cookie；
- Authorization Header；
- 数据库密码；
- 未脱敏的个人路径和账户信息；
- 含长期 CLI 凭证的目录副本。

---

## 26. 本课总结

本课最重要的不是记住某个 Provider 的字段，而是形成一套稳定的配置推理方法：

1. 先确定配置由哪个进程读取；
2. 再确定环境变量是否进入该进程；
3. 区分服务启动、Sandbox 隔离和 Agent 工作三类模式；
4. 用 `config.yaml` 声明模型、工具和运行时能力；
5. 用 `extensions_config.json` 管理 MCP、Skill 等可运行时扩展状态；
6. 用 `.env` 或部署平台 Secret 保存秘密；
7. 根据重启边界判断修改何时生效；
8. 用新 Run、日志、事件、Token 和产出文件验证行为；
9. 用控制变量实验选择模式，而不是根据名称判断强弱；
10. 把健康检查与最终任务正确性分开验收。

可以把本课压缩成一句话：

> **配置决定 Agent 被组装成什么，环境决定这些配置能否兑现，模式决定一次任务使用多少思考与组织能力，而证据决定我们是否真的知道它按预期运行。**

下一课将进入 LLM API 与消息协议，解释 System、Human、AI、Tool Message 如何组成一次真实模型调用，以及 Tool Calling 为什么必须经过“模型请求—确定性执行—结果回传”的闭环。

---

## 重要代码与文件

| 文件 | 本课用途 |
| --- | --- |
| `config.example.yaml` | 主配置完整模板与字段说明 |
| `extensions_config.example.json` | MCP、Skill 和扩展配置模板 |
| `.env.example` | 后端与 Compose 环境变量模板 |
| `frontend/.env.example` | Frontend 环境变量模板 |
| `Makefile` | 根目录启动、安装、诊断命令 |
| `scripts/configure.py` | `make config` 的真实生成行为 |
| `scripts/check.py` | 基础依赖检查逻辑 |
| `scripts/doctor.py` | DeerFlow 配置健康检查逻辑 |
| `scripts/serve.sh` | 本地开发与生产启动流程 |
| `backend/docs/CONFIGURATION.md` | 配置说明与安全注意事项 |
| `backend/packages/harness/deerflow/config/app_config.py` | 主配置定位、环境变量解析与校验 |
| `backend/packages/harness/deerflow/config/extensions_config.py` | 扩展配置定位与解析 |
| `backend/packages/harness/deerflow/config/reload_boundary.py` | 需要重启的配置项清单 |
| `frontend/src/core/threads/hooks.ts` | 四种 Agent 模式到请求上下文的映射 |
| `frontend/src/components/workspace/input-box.tsx` | 模式与推理深度选择界面 |
| `backend/packages/harness/deerflow/agents/lead_agent/agent.py` | 思考、规划、Sub-agent 和工具组装 |
| `docker/docker-compose.yaml` | Docker 生产服务与端口发布 |
| `docker/docker-compose-dev.yaml` | Docker 开发服务拓扑 |
| `docker/docker-compose.dood.yaml` | AIO/DooD 的 Docker Socket overlay |

## 关键配置速查

```text
DEER_FLOW_PROJECT_ROOT
  └─ 指定项目根目录

DEER_FLOW_CONFIG_PATH
  └─ 显式指定 config.yaml

DEER_FLOW_EXTENSIONS_CONFIG_PATH
  └─ 显式指定 extensions_config.json

DEER_FLOW_HOME
  └─ 指定 .deer-flow 运行状态目录

DEER_FLOW_SKILLS_PATH
  └─ skills.path 未设置时指定 Skills 目录

BIND_HOST / PORT
  └─ Docker 发布 Nginx 统一入口时的宿主监听地址和端口
```

## 四模式速查

```text
Flash
  thinking=false, plan=false, subagent=false

Thinking
  thinking=true, plan=false, subagent=false

Pro
  thinking=true, plan=true, subagent=false

Ultra
  thinking=true, plan=true, subagent=true
```
