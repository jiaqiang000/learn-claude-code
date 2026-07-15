# s19: MCP Tools — 外接工具，标准协议

[中文](README.md) · [English](README.en.md) · [日本語](README.ja.md)

s01 → ... → s17 → s18 → `s19` → [s20](../s20_comprehensive/)

> *"外接工具, 标准协议"* — 发现、组装、调用，Agent 不需要知道工具是谁写的。
>
> **Harness 层**: 插件 — 外部能力通过标准协议接入。

---

## 问题

s01 到 s18，Agent 的所有工具都是手写的——bash、read、write、task、worktree。每个工具的输入验证、执行逻辑、错误处理，都是你一行行写的。

现在你有 3 个外部服务想接入：公司的 Jira API（查 issue、建 ticket）、自建的部署系统（触发 deploy、看日志）、团队的 Notion 知识库（搜文档、建页面）。你不想为每个服务重写一套工具代码。

你需要一个标准协议——外部服务只要实现它，Agent 就能直接调用，不管服务用什么语言写的。

---

## 解决方案

![MCP Architecture](images/mcp-architecture.svg)

MCP（Model Context Protocol）定义了 Agent 如何发现和调用外部工具。核心概念：

| 概念 | 作用 |
|------|------|
| MCPClient | Agent 端的客户端，连接 server、发现工具、调用工具 |
| MCP Server | 外部服务，实现 `tools/list` + `tools/call` |
| assemble_tool_pool | 把内置工具和 MCP 工具组装成一个工具池 |
| mcp\_\_server\_\_tool 命名 | 避免不同 server 的工具名冲突 |

沿用 s18 的教学版 worktree 隔离、自主认领、空闲轮询、协议系统。本章新增：`connect_mcp` 工具——连接外部服务，发现工具，加入工具池。

教学版用 mock handler 模拟外部 server。真实版会启动子进程，通过 stdin/stdout 发送 JSON-RPC 请求。mock 的好处是不依赖外部服务就能跑完整流程；代价是你看不到真正的网络通信和进程管理。

---

## 工作原理

### MCPClient：发现 + 调用

```python
class MCPClient:
    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def register(self, tool_defs, handlers):
        """Simulates tools/list discovery."""
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> str:
        """Simulates tools/call."""
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        return handler(**args)
```

教学版用 Python 函数模拟 server 的工具实现。真实版通过 stdio JSON-RPC 与子进程通信。

### connect_mcp：连接 + 发现

```python
def connect_mcp(name: str) -> str:
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        return f"Unknown server '{name}'. Available: ..."
    mcp_client = factory()
    mcp_clients[name] = mcp_client
    return f"Connected to '{name}'. Discovered: ..."
```

连接后，server 提供的工具立即可用。

### normalize_mcp_name：名称规范化

```python
_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')

def normalize_mcp_name(name: str) -> str:
    return _DISALLOWED_CHARS.sub('_', name)
```

所有非 `[a-zA-Z0-9_-]` 的字符替换为 `_`。防止 server 名或工具名中包含特殊字符导致命名冲突或注入问题。

### assemble_tool_pool：组装工具池

```python
def assemble_tool_pool() -> tuple[list[dict], dict]:
    tools = list(BUILTIN_TOOLS)
    handlers = dict(BUILTIN_HANDLERS)
    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            safe_tool = normalize_mcp_name(tool_def["name"])
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            tools.append(...)
            handlers[prefixed] = (
                lambda *, c=mcp_client, t=tool_def["name"], **kw:
                    c.call_tool(t, kw))
    return tools, handlers
```

前缀 `mcp__{server}__{tool}` 避免不同 server 的工具名冲突。名称经过 `normalize_mcp_name` 规范化。

MCP 工具的 description 带 `(readOnly)` 或 `(destructive)` 标注——教学版用文本标注，真实 CC 用 tool annotations 结构体让权限系统判断。

### 无缓存：工具池变了，prompt 也变

s10-s18 的 agent_loop 用 prompt cache 避免重复序列化。s19 去掉了缓存：

```python
def agent_loop(messages, context):
    tools, handlers = assemble_tool_pool()     # 每次重新构建
    system = assemble_system_prompt(context)    # 每次重新生成
    ...
    if any(b.name == "connect_mcp" ...):
        tools, handlers = assemble_tool_pool()  # 连接后重建
        system = assemble_system_prompt(context)
```

原因：`connect_mcp` 之后工具池变化了——新增了 `mcp__docs__search` 等工具。缓存中的工具列表是旧的，继续用会导致模型调用不到新工具。教学版直接去掉缓存，代价是多花一点序列化时间。

### MCP 工具只有 Lead 可用

教学版中，`connect_mcp` 是 Lead 工具，`assemble_tool_pool` 也只服务于 Lead 的 agent_loop。Teammate 仍使用固定的 8 个子集工具（bash、read_file、write_file、send_message、submit_plan、list_tasks、claim_task、complete_task）。

这是教学简化。真实 CC 中，MCP 工具对主 agent 和子 agent 都可用——子 agent 继承父级的 MCP 配置。

---

## 相对 s18 的变更

| 组件 | 之前 (s18) | 之后 (s19) |
|------|-----------|-----------|
| 工具来源 | 全部手写 builtin | 手写 + MCP 外部工具动态发现 |
| 工具池 | 固定 BUILTIN_TOOLS | assemble_tool_pool 动态组装 mcp\_\_ 前缀工具 |
| 名称安全 | 无 | normalize_mcp_name 规范化 |
| 新类型 | — | MCPClient 类（模拟 tools/list + tools/call） |
| 命名空间 | — | mcp\_\_server\_\_tool 避免冲突 |
| 工具描述 | 无标注 | (readOnly)/(destructive) 标注 |
| prompt 缓存 | 有（s10 起） | 去掉——工具池动态变化后缓存失效 |
| Lead 工具 | 17 (s18) | 18 (+connect_mcp) |
| Teammate 工具 | 8 (s18) | 8（不变，MCP 工具仅 Lead 可用） |
| 扩展方式 | 写代码加工具 | 标准协议，任意语言实现 server |

---

## 试一下

```sh
cd learn-claude-code
python s19_mcp_plugin/code.py
```

试试这些 prompt：

1. `Connect to the docs MCP server and search for something`
2. `Connect to the deploy server and trigger a deployment`
3. `Connect both servers — what tools are now available?`

观察重点：连接 MCP server 后，工具名是否带 `mcp__docs__` 或 `mcp__deploy__` 前缀？两个 server 的工具是否同时可用？MCP 工具的 description 是否带 (readOnly)/(destructive) 标注？

---

## 接下来

现在 Agent 可以通过标准协议接入外部工具了。但前面 19 章每章都只加一个机制，真实 Agent 不会这样拆开运行。

工具、权限、hooks、todo、任务图、记忆、压缩、后台、cron、团队、worktree、MCP 这些机制应该挂在同一个循环上，而不是散在 19 个 demo 里。

s20 Comprehensive Agent → 把前 19 章的机制合回一个完整 harness。机制很多，循环一个。

<details>
<summary>深入 CC 源码</summary>

> 以下基于 CC 源码 `services/mcp/client.ts`、`auth.ts`、`config.ts`、`channelNotification.ts` 的分析。

### 一、6 种 Transport 类型

教学版只展示了 stdio mock。CC 支持 6 种传输（`types.ts:23-25`）：

| Transport | 通信方式 |
|-----------|---------|
| `stdio` | 子进程 stdin/stdout（跨平台默认） |
| `sse` | HTTP Server-Sent Events |
| `http` | Streamable HTTP（POST/SSE 双向） |
| `ws` | WebSocket |
| `sse-ide` | IDE 内嵌 SSE 传输 |
| `sdk` | 进程内 SDK 传输 |

连接时本地（stdio）和远程（http/sse/ws）服务器分批并发：本地批量 3 个，远程批量 20 个。

### 二、工具池组装算法

`assembleToolPool()`（`tools.ts:345-364`）：

```typescript
// 去重时优先保留内置工具（name 相同时内置在前）
return uniqBy(
  [...builtInTools.sort(byName), ...filteredMcpTools.sort(byName)],
  'name',
)
```

内置工具和 MCP 工具分开排序，不是合起来排。原因是 CC 的 `claude_code_system_cache_policy` 在最后一个内置工具之后的某个位置放全局缓存断点——混排会破坏这个设计。

### 三、命名规则：`mcp__server__tool`

`buildMcpToolName()`（`mcpStringUtils.ts:50-52`）：

```
mcp__<normalizedServerName>__<normalizedToolName>
```

所有非 `[a-zA-Z0-9_-]` 字符替换为 `_`（`normalization.ts:17-23`）。教学版的 `normalize_mcp_name` 用同样的规则。

### 四、权限检查

CC 对 MCP 工具有独立的权限系统。`checkPermissions()` 对 MCP 工具的检查逻辑不同于内置工具——MCP 工具可以声明自己的权限需求（readOnly、destructive 等），CC 根据声明决定是否需要用户确认。教学版只在 description 中用文本标注 `(readOnly)` / `(destructive)`，不做权限拦截。

### 五、配置来源与优先级

MCP 服务器配置来自多个来源。CC 的配置优先级从低到高：

```
claude.ai 连接器 < plugin < user settings.json < approved project .mcp.json < local settings.local.json
```

`claude.ai` 连接器单独拉取、按内容签名去重，以最低优先级合并（`config.ts:1267-1289`）。企业 `managed-mcp.json` 存在时完全排除其他配置。

教学版直接传 server name 给 `MOCK_SERVERS` 字典，不做配置合并。

### 六、Channel 通知：服务器反向推消息

教学版只讲了 Agent → MCP Server 的单向调用。CC 还支持反向通知（`channelNotification.ts`）：

1. Server 声明 `capabilities.experimental['claude/channel']`
2. Server 通过 MCP 通知 `notifications/claude/channel` 给 Agent 发消息
3. 消息包装在 `<channel source="serverName">...</channel>` XML 标签中
4. Agent 被 SleepTool 唤醒（1 秒内）

Server 还可以请求权限：`notifications/claude/channel/permission_request` → Agent 回复 `notifications/claude/channel/permission`。用户通过 5 字母短 ID 确认/拒绝。

### 七、OAuth 认证流程

CC 的 MCP 认证（`auth.ts`）支持完整的 OAuth 2.0 + PKCE 流程：
- 通过公钥客户端 + PKCE 发现 OAuth 元数据（RFC 8414 / RFC 9728）
- 本地回调服务器接收授权码
- 令牌通过 `getSecureStorage()` 持久化（macOS Keychain / Linux 加密文件 / Windows 凭据管理器）
- 过期前 5 分钟自动刷新
- 支持跨应用访问（XAA）：浏览器获取 id_token → RFC 8693 + RFC 7523 交换 → 无需反复弹浏览器

### 八、连接生命周期的错误处理

CC 对 MCP 连接有精细的错误分类和重试（`client.ts:1266-1402`）：
- 终局性错误（ECONNRESET、ETIMEDOUT、EPIPE 等）：连续 3 次 → 关闭 + 重连
- 工具调用 401：令牌过期 → 抛出 `McpAuthError` → 触发重认证
- 工具调用超时：`Promise.race` 超时（可配置，默认约 28 小时）
- Stdio 断连：按 SIGINT → SIGTERM → SIGKILL 顺序杀进程

### 教学版的简化

- 6 种 transport → 1 种（mock stdio）：概念量可控
- Channel 反向通知 → 省略：教学版 Agent 是主动方
- OAuth 流程 → 省略：教学版假设 server 不需要认证
- 多层配置优先级 → 省略：教学版直接传 server name
- 复杂的错误分类 → 省略：教学版用 try/except 兜底
- MCP 工具只给 Lead → 省略子 agent 继承：简化代码结构

</details>

<!-- translation-sync: zh@v2, en@v0, ja@v0 -->

> 懂。你要的是：
>
> > **先把一条完整流程讲通，只保留支撑这条流程的少量核心名词；名词第一次出现时顺手解释，内部函数名统一放到最后做源码对应。**
>
> 不是把源码名全部搬进正文，也不是把所有名词都删掉。
>
> 这部分更合适的讲法如下。
>
> ---
>
> ## OAuth 在 MCP 连接里的位置
>
> Claude Code 连接一个远程 MCP 服务时，首先尝试直接建立连接。
>
> ```text
> 连接 MCP 服务
>     ↓
> 服务检查当前用户身份
>     ↓
> 已有有效登录凭证 → 连接成功
> 没有登录凭证     → 要求用户认证
> ```
>
> 这里的“登录凭证”叫 **token**。可以先把它理解成：
>
> > 服务发给 Claude Code 的一张临时电子通行证。
>
> Claude Code 以后每次访问这个 MCP 服务，都会携带这张通行证。服务看到后，才能确认用户身份和权限。
>
> ---
>
> ## 首次认证的完整过程
>
> 第一次连接时，本地还没有通行证，于是进入下面的过程：
>
> ```text
> MCP 服务要求认证
>     ↓
> Claude Code 找到该服务的登录地址
>     ↓
> 打开浏览器
>     ↓
> 用户登录并确认授权
>     ↓
> 浏览器把一次性授权码送回 Claude Code
>     ↓
> Claude Code 用授权码换取 token
>     ↓
> 保存 token
>     ↓
> 重新连接 MCP 服务
> ```
>
> 这一整段就是普通 OAuth 的主体。
>
> ### 1. 找到登录入口
>
> MCP 服务本身不一定负责账号登录，它可能把登录交给另一个认证服务。
>
> 所以 Claude Code 需要先知道：
>
> ```text
> 登录页面在哪里
> 换取 token 的接口在哪里
> 支持哪些授权范围
> ```
>
> README 里的“发现 OAuth 元数据”，就是自动读取这些连接信息。
>
> 源码中，这部分由 `auth.ts` 负责，并按照 RFC 9728、RFC 8414 查找认证服务及其接口。
>
> ---
>
> ### 2. 浏览器完成用户登录
>
> Claude Code 打开浏览器后，用户在网页里登录自己的账号并同意授权。
>
> 同时，Claude Code 会在本机临时开启一个回调地址：
>
> ```text
> http://127.0.0.1:某个端口/callback
> ```
>
> 登录完成后，浏览器会跳转到这个地址，并附带一个一次性授权码：
>
> ```text
> 浏览器
>     ↓
> 本地 callback 地址
>     ↓
> Claude Code 收到授权码
> ```
>
> 源码确实会创建本地 HTTP Server，接收 `/callback` 请求，并读取其中的 `code`。收到后临时 Server 就会被关闭。
>
> ---
>
> ### 3. PKCE 保护授权码
>
> 浏览器返回的授权码不能单独使用。
>
> 认证开始前，Claude Code 会生成一份只保存在本地的随机值。浏览器登录开始时，只把它计算后的结果交给认证服务；登录完成后，再提交原始值进行核对。
>
> 可以理解成：
>
> ```text
> 开始登录：
> Claude Code 先设置一个临时暗号
>
> 登录结束：
> 授权码 + 正确暗号
>         ↓
> 才能换取 token
> ```
>
> 这个机制叫 **PKCE**。
>
> 这样，其他程序即使截获了浏览器返回的授权码，因为没有 Claude Code 本地保存的暗号，也无法冒用。
>
> 源码中会保存这份校验值，并在完成授权码交换时再次取出。
>
> ---
>
> ## token 进入正常使用阶段
>
> 授权码验证成功后，认证服务通常返回两类 token：
>
> ```text
> access token
> refresh token
> ```
>
> 它们在整个流程中的位置是：
>
> ```text
> access token
>     ↓
> 每次访问 MCP 服务时携带
>     ↓
> 证明当前请求已经得到授权
> ```
>
> ```text
> refresh token
>     ↓
> access token 即将失效时使用
>     ↓
> 换取一张新的 access token
> ```
>
> 因此，access token 是日常使用的通行证，refresh token 是更新通行证的凭据。
>
> 源码会同时保存：
>
> ```text
> access token
> refresh token
> 失效时间
> 权限范围
> ```
>
> ---
>
> ## 保存后重新回到 MCP 主流程
>
> token 保存完成后，Claude Code 会重新连接刚才失败的 MCP 服务：
>
> ```text
> token 保存完成
>     ↓
> 重新连接 MCP 服务
>     ↓
> 请求中携带 access token
>     ↓
> 服务确认身份
>     ↓
> 连接成功
>     ↓
> 获取服务提供的工具
>     ↓
> 工具加入 Agent 工具池
> ```
>
> 所以认证不是 MCP 之外的独立系统，而是夹在“连接服务”和“发现工具”之间：
>
> ```text
> 连接 MCP
>     ↓
> 身份认证
>     ↓
> 连接成功
>     ↓
> 发现工具
>     ↓
> 调用工具
> ```
>
> `client.ts` 中，HTTP 和 SSE 连接都会读取 token，并放入 `Authorization: Bearer ...` 请求头。
>
> ---
>
> ## 后续启动与自动续期
>
> 下次启动 Claude Code 时，会先读取之前保存的 token：
>
> ```text
> 读取本地 token
>     ↓
> 仍然有效
>     ↓
> 直接连接，不打开浏览器
> ```
>
> access token 通常有有效期。源码会记录它的失效时间，并在剩余时间不足五分钟时，使用 refresh token 提前换取新的 access token：
>
> ```text
> 旧 access token 即将失效
>     ↓
> 使用 refresh token
>     ↓
> 取得新 access token
>     ↓
> 更新本地记录
>     ↓
> 连接继续使用
> ```
>
> 所以“过期前五分钟刷新”只是 token 生命周期里的维护步骤，不是另一套登录机制。源码中的判断条件就是剩余时间不超过 `300` 秒。
>
> 如果 access token 已失效，同时也没有 refresh token，Claude Code 才会重新进入浏览器认证流程。
>
> ---
>
> ## XAA 接在这条流程的哪里
>
> 普通 OAuth 针对每个 MCP 服务分别认证：
>
> ```text
> 连接 Jira
> → 浏览器登录授权
> → 得到 Jira 的 access token
>
> 连接另一个服务
> → 再次浏览器登录授权
> → 得到另一个 access token
> ```
>
> XAA 把前半段改成：
>
> ```text
> 先登录一次企业统一账号
>     ↓
> 得到一份身份凭证
>     ↓
> 为不同 MCP 服务分别交换它们自己的 access token
> ```
>
> 这里保留两个核心名词就够了：
>
> - **id token**：证明“当前用户是谁”
> - **access token**：证明“当前用户可以访问某个具体服务”
>
> 因此，XAA 的完整位置是：
>
> ```text
> 企业账号登录一次
>     ↓
> 得到 id token
>     ↓
> 为 Jira 换取 Jira access token
>     ↓
> 为另一个服务换取它自己的 access token
>     ↓
> 分别连接这些 MCP 服务
> ```
>
> 不是把同一个 access token 给所有服务共用，而是复用一次企业身份，再为每个服务生成对应的访问凭证。
>
> 源码中的交换链是：
>
> ```text
> id token
> → 中间身份断言
> → 目标 MCP 服务的 access token
> ```
>
> ---
>
> ## 最终只需要记住这一条线
>
> ```text
> 首次连接远程 MCP 服务
>         ↓
> 服务要求认证
>         ↓
> 浏览器登录
>         ↓
> 浏览器返回一次性授权码
>         ↓
> 授权码经过 PKCE 校验
>         ↓
> 换取 access token 和 refresh token
>         ↓
> 保存到本地
>         ↓
> 重新连接 MCP 服务
>         ↓
> 发现并调用工具
>         ↓
> access token 快失效时自动刷新
> ```
>
> XAA 只是把其中的“每个服务都打开一次浏览器”改成：
>
> ```text
> 企业身份登录一次
> → 为多个服务分别交换 access token
> ```
>
> 源码函数名以后只在流程讲清楚之后集中对应，不再让它们打断正文。
