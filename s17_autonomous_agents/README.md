# s17: Autonomous Agents — 自己看板，自己认领

[中文](README.md) · [English](README.en.md) · [日本語](README.ja.md)

s01 → ... → s15 → s16 → `s17` → [s18](../s18_worktree_isolation/) → s19 → s20

> *"自己看板，自己认领"* — 空闲时轮询，有活就干。
>
> **Harness 层**: 自治 — 队友自组织，不依赖 Lead 分配。

---

## 问题

s16 的队友能通信、能握手关机。但每个队友等 Lead 分配任务——如果任务看板上有 10 个未认领任务，Lead 得手动 assign 10 次。这不能扩展。队友应该自己看任务看板，发现没人做的任务就认领，做完再找下一个。

---

## 解决方案

![Autonomous Agents Overview](images/autonomous-agents-overview.svg)

> ### s17 核心流程总结
>
> s17 给每个队友线程增加了一层明确的生命周期循环：
>
> ```text
> WORK → IDLE → 找到工作后回到 WORK → 超时或收到关机请求后 SHUTDOWN
> ```
>
> 核心变化是：队友做完当前一轮工作后，不再只依赖 Lead 继续派活，而是会在 IDLE 阶段主动检查消息和共享任务板。
>
> #### 1. WORK：执行当前工作
>
> 队友在 `spawn_teammate_thread()` 内部运行自己的 LLM/tool loop：
>
> ```text
> 检查 inbox
> → 调用 LLM
> → LLM 返回 tool_use
> → sub_handlers 执行工具
> → tool_result 回填 messages
> → 再次调用 LLM
> ```
>
> 一次 WORK 最多执行 10 轮：
>
> ```python
> for _ in range(10):
> ```
>
> 当模型不再返回 tool_use 时：
>
> ```python
> if response.stop_reason != "tool_use":
>     break
> ```
>
> 表示当前这一轮 WORK 结束，接下来进入 IDLE。
>
> 这里的“WORK 结束”不一定代表任务已经调用了 `complete_task`，只是模型暂时不再调用工具。
>
> #### 2. IDLE：等待消息或主动寻找任务
>
> WORK 结束后调用：
>
> ```python
> idle_result = idle_poll(name, messages, role)
> ```
>
> `idle_poll()` 每 5 秒检查一次，最多等待 60 秒：
>
> ```python
> IDLE_POLL_INTERVAL = 5
> IDLE_TIMEOUT = 60
> ```
>
> 检查顺序固定为：
>
> - 先检查 inbox
> - 再扫描任务板
>
> IDLE 中有以下几种情况。
>
> **情况一：收到 shutdown_request**
>
> 队友先回复 Lead：
>
> ```text
> shutdown_response
> ```
>
> 然后：
>
> ```python
> return "shutdown"
> ```
>
> 外层生命周期收到 "shutdown" 后退出。
>
> 因此，队友无论处于 WORK 还是 IDLE，都能响应关机请求：
>
> ```text
> WORK：handle_inbox_message 处理
> IDLE：idle_poll 直接处理
> ```
>
> **情况二：收到普通消息**
>
> 例如 Lead 发来新的要求，`idle_poll()` 会把消息写进队友自己的 messages：
>
> ```python
> messages.append({
>     "role": "user",
>     "content": "<inbox>...</inbox>"
> })
> ```
>
> 然后返回：
>
> ```python
> return "work"
> ```
>
> 外层 `while True` 开始下一轮，队友重新进入 WORK，LLM 就能看到这条新消息。
>
> 完整链条是：
>
> ```text
> IDLE 收到消息
> → 注入 messages
> → 返回 work
> → 再次进入 WORK
> → LLM 处理新消息
> ```
>
> **情况三：没有消息，扫描任务板**
>
> 如果 inbox 为空，则执行：
>
> ```python
> unclaimed = scan_unclaimed_tasks()
> ```
>
> 它只寻找同时满足三个条件的任务：
>
> - status == pending
> - owner 为空
> - blockedBy 中的依赖全部完成
>
> 对应代码：
>
> ```python
> if (
>     task.get("status") == "pending"
>     and not task.get("owner")
>     and can_start(task["id"])
> ):
>     unclaimed.append(task)
> ```
>
> 其中，任务有 blockedBy 并不代表不能做；只要所有依赖任务都已经 completed，`can_start()` 就会返回 True。
>
> 发现任务后，并不会直接认为认领成功，而是再次调用：
>
> ```python
> result = claim_task(task["id"], name)
> ```
>
> `claim_task()` 会重新检查：
>
> - 任务是否仍为 pending
> - 是否已经有 owner
> - 依赖是否仍然满足
>
> 成功后修改为：
>
> ```text
> owner = 当前队友名
> status = in_progress
> ```
>
> 扫描和认领之所以分开，是因为 Alice 和 Bob 可能同时看到同一个未认领任务。二次检查能够减少重复认领，但教学版没有文件锁，因此仍不能保证真正的原子操作。
>
> 认领成功后，Python 还会向队友的对话历史注入：
>
> ```text
> <auto-claimed>
> Task task_xxx: 任务名称
> </auto-claimed>
> ```
>
> 然后返回 "work"：
>
> ```text
> 扫描到任务
> → claim_task 成功
> → 修改任务状态
> → 注入 auto-claimed 消息
> → 返回 work
> → 重新进入 WORK
> → LLM 开始执行该任务
> ```
>
> **情况四：60 秒没有新工作**
>
> 如果连续 60 秒：
>
> - 没有 inbox 消息
> - 没有可认领任务
>
> 则：
>
> ```python
> return "timeout"
> ```
>
> 外层生命周期收到 "timeout" 后进入 SHUTDOWN。
>
> #### 3. 完成任务后继续找下一个
>
> 在 WORK 中，LLM 可以调用：
>
> ```python
> complete_task(task_id)
> ```
>
> 把当前任务修改为：
>
> ```text
> status = completed
> ```
>
> 如果它解锁了后续任务，`complete_task()` 会提示哪些任务现在可以执行，但它不会直接认领下一任务。
>
> 后续仍然经过完整流程：
>
> ```text
> complete_task(A)
> → 当前 WORK 结束
> → 进入 IDLE
> → scan_unclaimed_tasks()
> → 发现依赖 A 的任务 B 已解锁
> → claim_task(B)
> → 再次进入 WORK
> ```
>
> 因此几个函数的职责是分开的：
>
> - `complete_task`  
>   更新完成状态、解除依赖
> - `scan_unclaimed_tasks`  
>   找出当前可以执行的任务
> - `claim_task`  
>   将任务归属到具体队友
> - WORK  
>   让 LLM 真正执行任务

沿用 S16 的教学版 MessageBus 和协议工具。本章新增：**idle_poll**（空闲时每 5 秒轮询一次）、**scan_unclaimed_tasks**（扫描看板上可认领的任务）、**自动认领**（找到任务就 claim，不用 Lead 操心）。

队友生命周期从两阶段变成三阶段：

| 阶段 | 行为 | 退出条件 |
|------|------|---------|
| WORK | inbox → LLM → 工具循环 | `stop_reason != tool_use` |
| IDLE | 每 5s 轮询 inbox + 任务板 | 60s 超时 |
| SHUTDOWN | 发 summary，退出 | — |

---

## 工作原理

### idle_poll: 空闲轮询

队友完成当前任务后不退出，进入 IDLE 阶段——每 5 秒检查一次有没有新工作：

```python
IDLE_POLL_INTERVAL = 5   # seconds
IDLE_TIMEOUT = 60         # seconds

def idle_poll(name, messages, role) -> str:
    """Return 'work', 'shutdown', or 'timeout'."""
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)

        # ① 检查收件箱（优先）
        inbox = BUS.read_inbox(name)
        if inbox:
            # shutdown_request 立即处理
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    # ... 回复 shutdown_response
                    return "shutdown"
            # 普通消息注入上下文，回到 WORK
            messages.append(...)
            return "work"

        # ② 扫描任务看板
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task = unclaimed[0]
            result = claim_task(task["id"], name)
            if "Claimed" in result:
                messages.append(...)
                return "work"
    return "timeout"
```

inbox 优先（可能包含 shutdown_request 等协议消息），任务板其次。IDLE 阶段收到 shutdown_request 会直接回复并退出，不等到下一轮 WORK。

### scan_unclaimed_tasks: 扫描任务看板

找 pending 状态、无 owner、所有依赖已完成（`can_start`）的任务：

```python
def scan_unclaimed_tasks() -> list[dict]:
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed
```

三个条件：必须是 pending、没有 owner、所有 blockedBy 依赖已完成。`can_start` 检查依赖任务的状态——有依赖不代表不能做，只有被未完成的任务阻塞才不能做。教学版按文件名排序取第一个；CC 用文件锁防止多个队友同时认领同一个任务。

### claim_task: owner 检查

自动认领时检查 claim 结果，不把失败当成功：

```python
def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    return f"Claimed {task.id} ({task.subject})"
```

教学版没有文件锁，并发认领可能出现竞争。但至少 `task.owner` 检查避免了最明显的"后写覆盖"问题。CC 用 `proper-lockfile` 保护任务文件，`claimTask` 在文件锁内完成读-改-写（`utils/tasks.ts:541-612`）。

### 队友生命周期: WORK → IDLE → SHUTDOWN

s16 的队友做完任务就退出。s17 加了 IDLE 阶段，队友在外层循环中反复 WORK → IDLE：

```python
# Outer loop: WORK → IDLE cycle
while True:
    # WORK phase: 内层循环（最多 10 轮 LLM 调用）
    for _ in range(10):
        # 检查 inbox、处理协议消息、调 LLM、执行工具
        ...
        if response.stop_reason != "tool_use":
            break  # WORK 阶段结束

    # IDLE phase
    idle_result = idle_poll(name, messages, role)
    if idle_result == "shutdown":
        break
    if idle_result == "timeout":
        break  # 60s 超时 → SHUTDOWN

# SHUTDOWN: 发 summary 给 Lead
BUS.send(name, "lead", summary, "result")
```

关键设计：
- **外层 while True**：WORK 和 IDLE 交替进行，直到超时或收到关机请求
- **内层 for 10**：WORK 阶段最多 10 轮 LLM 调用（防止无限循环）
- **IDLE 超时 60 秒**：12 次轮询 × 5 秒 = 60 秒。超时后发送 summary 并退出
- **shutdown_request 两阶段都能响应**：WORK 阶段通过 `handle_inbox_message` 分发；IDLE 阶段 `idle_poll` 直接检查并回复

### 身份重注入

autoCompact（s08）之后，队友的 messages 列表可能被压缩成一段摘要。每次进入新的 WORK 阶段时检查：

```python
if len(messages) <= 3:
    messages.insert(0, {"role": "user",
        "content": f"<identity>You are '{name}', role: {role}. "
                   f"Continue your work.</identity>"})
```

消息过短说明发生了压缩，此时重新注入身份信息。真实 CC 中 context compaction 会保留 system prompt，教学版的简化实现需要手动处理。

### consume_lead_inbox: 统一 inbox 消费

`check_inbox` 工具和主循环末尾都调用同一个 `consume_lead_inbox()` 函数：先路由协议 response 更新状态，再把所有消息注入 Lead 的对话历史。队友发来的 summary/result 不会只打印在终端，Lead 的 LLM 能看到并协调下一步。

### 合起来跑

```
1. Lead: "搭建后端——任务太多，让队友自己认领"
2. Lead → create_task("创建数据库 schema")
3. Lead → create_task("写 API 路由")
4. Lead → create_task("写单元测试")
5. Lead → spawn_teammate("alice", "backend", "你是后端开发者")
6. Lead → spawn_teammate("bob", "backend", "你是后端开发者")

7. alice 线程启动 → WORK: 没有初始 inbox → 空转 → IDLE
8. bob 线程启动 → WORK: 没有初始 inbox → 空转 → IDLE

9. alice IDLE 第 1 次轮询 → scan_unclaimed → 发现"创建数据库 schema"
10. alice → claim_task → "创建数据库 schema" → 回到 WORK
11. bob IDLE 第 1 次轮询 → scan_unclaimed → 发现"写 API 路由"
12. bob → claim_task → "写 API 路由" → 回到 WORK

13. alice WORK: write_file("schema.sql", ...) → complete_task → WORK 结束
14. alice IDLE → scan → "写单元测试" → claim → WORK
15. alice WORK: write_file("test_api.py", ...) → complete_task → WORK 结束
16. alice IDLE → 60s 无新任务 → SHUTDOWN

17. bob 类似流程 → 做完 → SHUTDOWN
18. Lead consume_lead_inbox → 看到 alice 和 bob 的 summary
```

两个队友并行认领、并行工作。Lead 只需要创建任务和启动队友，不需要手动分配。

---

## 相对 s16 的变更

| 组件 | 之前 (s16) | 之后 (s17) |
|------|-----------|-----------|
| 任务分配 | Lead 手动 assign | 队友自动认领（can_start 检查依赖） |
| 队友状态 | WORK 或退出 | WORK → IDLE（轮询 60s） → SHUTDOWN |
| claim_task | 无 owner 检查 | 拒绝已有 owner 的任务 |
| IDLE 阶段关机 | 不处理 shutdown_request | 直接 dispatch shutdown 并退出 |
| Lead inbox | 只打印，不进上下文 | consume_lead_inbox 统一注入 history |
| 新函数 | — | idle_poll, scan_unclaimed_tasks, consume_lead_inbox |
| 身份保持 | 仅 system prompt | 压缩后自动重注入 |
| Lead 工具 | 14 (s16) | 14（不变） |
| 队友工具 | 5 | 8（+ list_tasks, claim_task, complete_task） |
| 队友退出条件 | 完成任务即退出 | 60s 无新任务才退出 |

---

## 试一下

```sh
cd learn-claude-code
python s17_autonomous_agents/code.py
```

试试这个 prompt：

`Create 3 tasks on the board, then spawn alice and bob. Watch them auto-claim and work.`

观察重点：队友是否自动认领了未分配的任务？有 blockedBy 依赖的任务是否在前置完成后被正确认领？空闲超时后是否自动关机？IDLE 阶段收到 shutdown_request 是否立即响应？`.tasks/` 目录下的任务状态如何变化？

---

## 接下来

队友自组织了。但 Alice 和 Bob 都在同一个目录下工作——Alice 改 `config.py`，Bob 也改 `config.py`，互相覆盖。

s18 Worktree Isolation → 每个任务有自己的工作目录，互不干扰。

<details>
<summary>深入 CC 源码</summary>

> 教学说明：本章的 idle_poll + auto-claim 机制是教学设计，用统一的轮询函数演示"空闲后找活干"。CC 的实际实现是多个机制的组合，但目标一致——减少 Lead 的手动分配负担。

### 一、CC 的空闲机制：组合路径，不是单一轮询

教学版用一个 `idle_poll()` 统一处理空闲时的 inbox 检查和任务认领。CC 的实际实现是四个机制的组合：

**idle_notification**：队友完成一轮工作后，`sendIdleNotification()`（`inProcessRunner.ts:569-589`）向 Lead 发送空闲通知。Lead 知道队友可用了，可以分配新任务或请求关机。

**mailbox 轮询**：`waitForNextPromptOrShutdown()`（`inProcessRunner.ts:689-868`）是一个 **500ms 轮询循环**，持续检查三类来源：pending user messages、mailbox 文件消息、task list。shutdown_request 被优先处理（`inProcessRunner.ts:768-804`），不会被普通消息饿死。

**task watcher**：`useTaskListWatcher`（`hooks/useTaskListWatcher.ts:34-189`）用 `fs.watch()` 监听 `.claude/tasks/` 目录变化，1 秒 debounce，当新任务创建或依赖解锁时触发检查。依赖判断（`L197-207`）是"blockedBy 中没有未完成的任务"，不是"blockedBy 为空"。

**主动 claim**：轮询循环内部也会调用 `tryClaimNextTask()`（`inProcessRunner.ts:853-860`）——在等待期间主动从 task list 领取任务。所以"队友不主动轮询任务"不准确，CC 同时有被动通知和主动认领。

### 二、任务认领：文件锁 + 原子操作

`claimTask()`（`utils/tasks.ts:541-612`）用 `proper-lockfile` 的任务文件锁，在锁内完成读-检查-改-写。检查项：owner 是否已存在（`L575-576`）、是否已完成（`L580-581`）、blockedBy 中是否有未完成任务（`L585-594`）。`claimTaskWithBusyCheck()`（`utils/tasks.ts:614-692`）用 task-list 级别锁，把 busy check 和 claim 做成原子操作，避免 TOCTOU。

`findAvailableTask()`（`inProcessRunner.ts:595-604`）的依赖判断也是"所有 blockedBy 已完成"，用 `task.blockedBy.every(id => !unresolvedTaskIds.has(id))` 实现。`tryClaimNextTask()`（`inProcessRunner.ts:624-657`）在认领后把状态更新为 `in_progress`，让 UI 立即反映变化。

### 三、教学版 vs CC 对比

| 维度 | 教学版 (s17) | CC |
|------|-------------|-----|
| 空闲机制 | idle_poll 统一轮询（5s） | idle_notification + 500ms mailbox 轮询 + task watcher |
| 任务发现 | scan_unclaimed_tasks（轮询） | useTaskListWatcher（文件监听）+ tryClaimNextTask（主动轮询） |
| 依赖判断 | can_start（所有 blockedBy 已完成） | findAvailableTask（同样语义） |
| 并发安全 | owner 检查（无文件锁） | proper-lockfile 任务锁 + task-list 锁 |
| shutdown 处理 | IDLE 直接分发，WORK 通过 handle_inbox_message | 500ms 轮询中优先处理 shutdown_request |
| 超时退出 | 60s 无新任务 | 无固定超时，Lead 手动 shutdown |
| 身份保持 | messages 长度检测 | context compaction 保留 system prompt |
| claim 失败处理 | 检查返回值，失败不注入 | 文件锁保证原子性 |

教学版的 `idle_poll()` 把 CC 的四个机制合并成一个轮询函数——简化合理，因为核心语义（空闲时找活干、依赖解锁后可认领、shutdown 优先）是一致的。

</details>

<!-- translation-sync: zh@v2, en@v2, ja@v2 -->
