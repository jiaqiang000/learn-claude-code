#!/usr/bin/env python3
"""
s19: MCP Tools — MCPClient + tool discovery + assemble_tool_pool.

Run:  python s19_mcp_plugin/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s18:
  - MCPClient class: discovers tools, calls tools via mock handler
  - normalize_mcp_name: normalize tool/server names
  - assemble_tool_pool: assembles builtin + MCP tools into one pool
  - connect_mcp: connect to an MCP server, discover tools
  - Tool naming: mcp__{server}__{tool} with normalization
  - MCP tools have readOnly/destructive annotations
  - agent_loop uses dynamic tool pool (builtin + MCP), no prompt cache
  - Teammate tools: complete_task, worktree cwd (from s17/s18 fixes)

ASCII flow:
  connect_mcp("docs") → MCPClient discovers tools →
  assemble_tool_pool → [builtin... , mcp__docs__search, mcp__docs__get_version]
  agent_loop uses assembled pool
"""

# 本章目标：在 s18 的完整团队协作框架上，引入 MCP（Model Context Protocol）式的
# 外部工具接入机制。Agent 不再只能调用代码中预先手写的工具，而是可以先连接 MCP
# Server、发现其工具，再把这些工具动态加入同一个工具池。
#
# 本章核心流程：
#   1. Lead 先调用内置 connect_mcp 工具连接某个 Server；
#   2. MCPClient 保存 Server 暴露的工具定义与调用入口；
#   3. assemble_tool_pool 为外部工具添加 mcp__server__tool 命名空间，
#      并与原有内置工具、handler 合并；
#   4. agent_loop 检测到 connect_mcp 已执行后，立即重建工具池和 system prompt；
#   5. 下一轮模型请求即可直接调用刚发现的 MCP 工具。
#
# 教学版的 MCP Server 由本地 Python handler 模拟，不包含真实 stdio/HTTP JSON-RPC、
# OAuth、重连和权限拦截；但“发现工具 → 组装工具池 → 按统一方式调用”的主链路一致。

import os, subprocess, json, time, random, threading, re
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict, field

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# ═══════════════════════════════════════════════════════════
# FROM s12-s18 (unchanged): 文件化任务系统与依赖状态
# ═══════════════════════════════════════════════════════════
# 任务仍以 JSON 文件持久化，并通过 blockedBy、claim、complete 维护依赖与生命周期。
# s19 不修改任务调度逻辑；它只是为 Lead 增加新的外部工具来源。

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str
    owner: str | None
    blockedBy: list[str]
    worktree: str | None = None


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject, description=description,
        status="pending", owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task):
    _task_path(task.id).write_text(json.dumps(asdict(task), indent=2))


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text()))


def list_tasks() -> list[Task]:
    return [Task(**json.loads(p.read_text()))
            for p in sorted(TASKS_DIR.glob("task_*.json"))]


def get_task_json(task_id: str) -> str:
    return json.dumps(asdict(load_task(task_id)), indent=2)


def can_start(task_id: str) -> bool:
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if task.owner:
        return f"Task {task_id} already owned by {task.owner}"
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if _task_path(d).exists() and load_task(d).status != "completed"]
        missing = [d for d in task.blockedBy if not _task_path(d).exists()]
        parts = []
        if deps: parts.append(f"blocked by: {deps}")
        if missing: parts.append(f"missing deps: {missing}")
        return "Cannot start — " + ", ".join(parts)
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
    return msg


# ═══════════════════════════════════════════════════════════
# FROM s18 (unchanged): Git Worktree 隔离
# ═══════════════════════════════════════════════════════════
# 每个任务仍可绑定独立 worktree，避免多 Agent 同时修改同一工作目录。

WORKTREES_DIR = WORKDIR / ".worktrees"
WORKTREES_DIR.mkdir(exist_ok=True)

VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')


def validate_worktree_name(name: str) -> str | None:
    if not name:
        return "Worktree name cannot be empty"
    if name in (".", ".."):
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (f"Invalid worktree name '{name}': "
                "only letters, digits, dots, underscores, dashes (1-64 chars)")
    return None


def run_git(args: list[str]) -> tuple[bool, str]:
    try:
        r = subprocess.run(["git"] + args, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out[:5000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"


def log_event(event_type: str, worktree_name: str, task_id: str = ""):
    event = {"type": event_type, "worktree": worktree_name,
             "task_id": task_id, "ts": time.time()}
    events_file = WORKTREES_DIR / "events.jsonl"
    with open(events_file, "a") as f:
        f.write(json.dumps(event) + "\n")


def create_worktree(name: str, task_id: str = "") -> str:
    err = validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    path = WORKTREES_DIR / name
    if path.exists():
        return f"Worktree '{name}' already exists at {path}"
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    log_event("create", name, task_id)
    print(f"  \033[33m[worktree] created: {name} at {path}\033[0m")
    return f"Worktree '{name}' created at {path}"


def bind_task_to_worktree(task_id: str, worktree_name: str):
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)


def _count_worktree_changes(path: Path) -> tuple[int, int]:
    try:
        r1 = subprocess.run(["git", "status", "--porcelain"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        files = len([l for l in r1.stdout.strip().splitlines() if l.strip()])
        r2 = subprocess.run(["git", "log", "@{push}..HEAD", "--oneline"],
                            cwd=path, capture_output=True, text=True, timeout=10)
        commits = len([l for l in r2.stdout.strip().splitlines() if l.strip()])
        return files, commits
    except Exception:
        return -1, -1


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"Worktree '{name}' not found"
    if not discard_changes:
        files, commits = _count_worktree_changes(path)
        if files < 0:
            return "Cannot verify status. Use discard_changes=true to force."
        if files > 0 or commits > 0:
            return (f"Worktree '{name}' has {files} file(s), {commits} commit(s). "
                    "Use discard_changes=true or keep_worktree.")
    ok1, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok1:
        return f"Failed to remove worktree '{name}'"
    run_git(["branch", "-D", f"wt/{name}"])
    log_event("remove", name)
    print(f"  \033[33m[worktree] removed: {name}\033[0m")
    return f"Worktree '{name}' removed"


def keep_worktree(name: str) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    log_event("keep", name)
    return f"Worktree '{name}' kept for review (branch: wt/{name})"


# ═══════════════════════════════════════════════════════════
# FROM s10-s18 + NEW in s19: System Prompt 动态反映 MCP 连接状态
# ═══════════════════════════════════════════════════════════
# 基础 prompt 结构沿用此前章节；s19 新增两类 MCP 信息：
#   - 告诉模型 connect_mcp 的存在及 mcp__server__tool 命名规则；
#   - 连接成功后，把当前已连接的 Server 名称写入 prompt。

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "spawn_teammate, send_message, check_inbox, "
             "request_shutdown, request_plan, review_plan, "
             "create_worktree, remove_worktree, keep_worktree, "
             "connect_mcp. MCP tools are prefixed mcp__{server}__{tool}.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")
    # mcp_clients 是运行期注册表。连接 Server 后重新组装 prompt，模型就能看到
    # 当前环境中已经接入了哪些外部能力；未连接时不额外占用 prompt。
    mcp_names = list(mcp_clients.keys())
    if mcp_names:
        sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")
    return "\n\n".join(sections)


# ═══════════════════════════════════════════════════════════
# FROM s01-s18 (unchanged): 基础文件与 Shell 工具
# ═══════════════════════════════════════════════════════════
# 这些工具仍由本进程直接实现，是后面 assemble_tool_pool 中的 builtin 部分。

def safe_path(p: str, cwd: Path = None) -> Path:
    base = cwd or WORKDIR
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, cwd: Path = None) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=cwd or WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None, cwd: Path = None) -> str:
    try:
        lines = safe_path(path, cwd).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path = None) -> str:
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
# FROM s15-s18 (unchanged): Agent 消息总线
# ═══════════════════════════════════════════════════════════
# Lead 与 Teammate 继续通过各自的 JSONL 邮箱交换普通消息和协议消息。

MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)


class MessageBus:
    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict = None):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")
        print(f"  \033[33m[bus] {from_agent} → {to_agent}: "
              f"({msg_type}) {content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs


BUS = MessageBus()
active_teammates: dict[str, bool] = {}

# ═══════════════════════════════════════════════════════════
# FROM s16-s18 (unchanged): 团队协议请求状态
# ═══════════════════════════════════════════════════════════
# request_id 用来把 shutdown / plan 请求与异步返回的 response 对应起来。

@dataclass
class ProtocolState:
    request_id: str
    type: str
    sender: str
    target: str
    status: str
    payload: str
    created_at: float = field(default_factory=time.time)


pending_requests: dict[str, ProtocolState] = {}


def new_request_id() -> str:
    return f"req_{random.randint(0, 999999):06d}"


def match_response(response_type: str, request_id: str, approve: bool):
    state = pending_requests.get(request_id)
    if not state:
        return
    if state.type == "shutdown" and response_type != "shutdown_response":
        return
    if state.type == "plan_approval" and response_type != "plan_approval_response":
        return
    state.status = "approved" if approve else "rejected"


def consume_lead_inbox(route_protocol=True) -> list[dict]:
    msgs = BUS.read_inbox("lead")
    if route_protocol:
        for msg in msgs:
            meta = msg.get("metadata", {})
            req_id = meta.get("request_id", "")
            msg_type = msg.get("type", "")
            if req_id and msg_type.endswith("_response"):
                match_response(msg_type, req_id, meta.get("approve", False))
    return msgs


# ═══════════════════════════════════════════════════════════
# FROM s17-s18 (unchanged): Teammate 空闲轮询与自主认领
# ═══════════════════════════════════════════════════════════
# Teammate 完成当前工作后仍会进入 IDLE：轮询邮箱、主动寻找可认领任务，
# 收到关机请求或超过空闲时限后结束线程。

IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60


def scan_unclaimed_tasks() -> list[dict]:
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and can_start(task["id"])):
            unclaimed.append(task)
    return unclaimed


def idle_poll(agent_name: str, messages: list,
              name: str, role: str) -> str:
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "Shutting down.",
                             "shutdown_response",
                             {"request_id": req_id, "approve": True})
                    return "shutdown"
            messages.append({"role": "user",
                "content": "<inbox>" + json.dumps(inbox) + "</inbox>"})
            return "work"
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data["id"], agent_name)
            if "Claimed" in result:
                wt_info = ""
                if task_data.get("worktree"):
                    wt_info = f"\nWork directory: {WORKTREES_DIR / task_data['worktree']}"
                messages.append({"role": "user",
                    "content": f"<auto-claimed>Task {task_data['id']}: "
                               f"{task_data['subject']}{wt_info}</auto-claimed>"})
                return "work"
    return "timeout"


# ═══════════════════════════════════════════════════════════
# FROM s15-s18 (unchanged): Teammate 线程与固定子工具集
# ═══════════════════════════════════════════════════════════
# 教学版中 MCP 工具只加入 Lead 的动态工具池。Teammate 仍使用下面固定的 8 个工具，
# 因而不会继承 Lead 新连接的 docs/deploy 工具；真实 Claude Code 会做配置继承。

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"If a task has a worktree, work in that directory.")

    def handle_inbox_message(name: str, msg: dict, messages: list):
        msg_type = msg.get("type", "message")
        meta = msg.get("metadata", {})
        req_id = meta.get("request_id", "")
        if msg_type == "shutdown_request":
            BUS.send(name, "lead", "Shutting down.",
                     "shutdown_response",
                     {"request_id": req_id, "approve": True})
            return True
        if msg_type == "plan_approval_response":
            approve = meta.get("approve", False)
            messages.append({"role": "user",
                "content": "[Plan approved]" if approve
                           else f"[Plan rejected] {msg['content']}"})
        return False

    def run():
        wt_ctx = {"path": None}

        def _wt_cwd():
            p = wt_ctx["path"]
            return Path(p) if p else None

        def _run_bash(command: str) -> str:
            return run_bash(command, cwd=_wt_cwd())

        def _run_read(path: str) -> str:
            return run_read(path, cwd=_wt_cwd())

        def _run_write(path: str, content: str) -> str:
            return run_write(path, content, cwd=_wt_cwd())

        def _run_list_tasks():
            tasks = list_tasks()
            if not tasks:
                return "No tasks."
            return "\n".join(
                f"  {t.id}: {t.subject} [{t.status}]"
                + (f" (wt:{t.worktree})" if t.worktree else "")
                for t in tasks)

        def _run_claim_task(task_id: str):
            result = claim_task(task_id, owner=name)
            if "Claimed" in result:
                task = load_task(task_id)
                wt_ctx["path"] = (str(WORKTREES_DIR / task.worktree)
                                  if task.worktree else None)
            return result

        def _run_complete_task(task_id: str):
            result = complete_task(task_id)
            wt_ctx["path"] = None
            return result

        messages = [{"role": "user", "content": prompt}]
        # 这里是 Teammate 的固定工具定义。它没有调用 assemble_tool_pool，
        # 正是本教学版中“MCP 工具仅 Lead 可用”的代码落点。
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
            {"name": "submit_plan",
             "description": "Submit a plan for Lead approval.",
             "input_schema": {"type": "object",
                              "properties": {"plan": {"type": "string"}},
                              "required": ["plan"]}},
            {"name": "list_tasks",
             "description": "List all tasks.",
             "input_schema": {"type": "object", "properties": {},
                              "required": []}},
            {"name": "claim_task",
             "description": "Claim a pending task.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
            {"name": "complete_task",
             "description": "Mark an in-progress task as completed.",
             "input_schema": {"type": "object",
                              "properties": {"task_id": {"type": "string"}},
                              "required": ["task_id"]}},
        ]

        sub_handlers = {
            "bash": _run_bash, "read_file": _run_read,
            "write_file": _run_write,
            "send_message": lambda to, content: (BUS.send(name, to, content),
                                                  "Sent")[1],
            "submit_plan": lambda plan: _teammate_submit_plan(name, plan),
            "list_tasks": _run_list_tasks,
            "claim_task": _run_claim_task,
            "complete_task": _run_complete_task,
        }

        while True:
            if len(messages) <= 3:
                messages.insert(0, {"role": "user",
                    "content": f"<identity>You are '{name}', role: {role}. "
                               f"Continue your work.</identity>"})
            should_shutdown = False
            for _ in range(10):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    stopped = handle_inbox_message(name, msg, messages)
                    if stopped:
                        should_shutdown = True
                        break
                if should_shutdown:
                    break
                if inbox and not should_shutdown:
                    non_protocol = [m for m in inbox
                                    if m.get("type") == "message"]
                    if non_protocol:
                        messages.append({"role": "user",
                            "content": "<inbox>" + json.dumps(non_protocol) + "</inbox>"})
                try:
                    response = client.messages.create(
                        model=MODEL, system=system, messages=messages[-20:],
                        tools=sub_tools, max_tokens=8000)
                except Exception:
                    break
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        handler = sub_handlers.get(block.name)
                        output = handler(**block.input) if handler else "Unknown"
                        results.append({"type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": str(output)})
                messages.append({"role": "user", "content": results})
            if should_shutdown:
                break
            idle_result = idle_poll(name, messages, name, role)
            if idle_result in ("shutdown", "timeout"):
                break

        summary = "Done."
        for msg in reversed(messages):
            if msg["role"] == "assistant" and isinstance(msg["content"], list):
                for b in msg["content"]:
                    if getattr(b, "type", None) == "text":
                        summary = b.text
                        break
                else:
                    continue
                break
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)

    active_teammates[name] = True
    threading.Thread(target=run, daemon=True).start()
    return f"Teammate '{name}' spawned as {role}"


def _teammate_submit_plan(from_name: str, plan: str) -> str:
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="plan_approval",
        sender=from_name, target="lead",
        status="pending", payload=plan)
    BUS.send(from_name, "lead", plan,
             "plan_approval_request",
             {"request_id": req_id})
    return f"Plan submitted ({req_id})"


# ═══════════════════════════════════════════════════════════
# FROM s16-s18 (unchanged): Lead 的关机与计划审批工具
# ═══════════════════════════════════════════════════════════

def run_request_shutdown(teammate: str) -> str:
    req_id = new_request_id()
    pending_requests[req_id] = ProtocolState(
        request_id=req_id, type="shutdown",
        sender="lead", target=teammate,
        status="pending", payload="")
    BUS.send("lead", teammate, "Shut down.", "shutdown_request",
             {"request_id": req_id})
    return f"Shutdown request sent to {teammate}"


def run_request_plan(teammate: str, task: str) -> str:
    BUS.send("lead", teammate, f"Submit plan for: {task}", "message")
    return f"Asked {teammate} to submit a plan"


def run_review_plan(request_id: str, approve: bool,
                    feedback: str = "") -> str:
    state = pending_requests.get(request_id)
    if not state:
        return f"Request {request_id} not found"
    state.status = "approved" if approve else "rejected"
    BUS.send("lead", state.sender,
             feedback or ("Approved" if approve else "Rejected"),
             "plan_approval_response",
             {"request_id": request_id, "approve": approve})
    return f"Plan {'approved' if approve else 'rejected'}"


# ═══════════════════════════════════════════════════════════
# NEW in s19: MCP Client、工具发现与统一调用
# ═══════════════════════════════════════════════════════════
# MCPClient 位于 Agent/Harness 一侧：
#   - tools 保存从 Server “发现”到的工具元数据，相当于 tools/list 的结果；
#   - _handlers 保存教学版的本地实现，相当于 Server 端真正执行工具的入口；
#   - call_tool 按原始工具名转发参数，相当于一次 tools/call。
#
# 真实实现会通过标准传输协议把请求发给独立 MCP Server；这里用 Python 函数模拟，
# 目的是把重点放在工具发现、注册和 Agent 调用链路上。

class MCPClient:
    """Discovers and calls tools on an MCP server (mock for teaching)."""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, callable] = {}

    # 教学版把“发现到的定义”和“可执行 handler”一次性注册进客户端。
    # Agent 后续只依赖统一的 MCPClient 接口，不需要知道工具由谁、用什么语言实现。
    def register(self, tool_defs: list[dict],
                 handlers: dict[str, callable]):
        self.tools = tool_defs
        self._handlers = handlers

    # assemble_tool_pool 生成的外层 handler 最终都会回到这里，使用 Server 内部的
    # 原始工具名查找实现；异常在协议边界被转换成普通字符串，避免打断 Agent Loop。
    def call_tool(self, tool_name: str, args: dict) -> str:
        handler = self._handlers.get(tool_name)
        if not handler:
            return f"MCP error: unknown tool '{tool_name}'"
        try:
            return handler(**args)
        except Exception as e:
            return f"MCP error: {e}"


# 已连接 Server 的运行期注册表。connect_mcp 负责写入，工具池与 prompt 负责读取。
mcp_clients: dict[str, MCPClient] = {}

_DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


# Server 名和工具名来自外部，不能直接拼入模型可调用的工具名。
# 统一替换特殊字符可稳定命名空间，并降低名称冲突或注入特殊分隔符的风险。
def normalize_mcp_name(name: str) -> str:
    """Replace non [a-zA-Z0-9_-] with underscore."""
    return _DISALLOWED_CHARS.sub('_', name)


# 两个 factory 模拟独立 MCP Server。tool_defs 是发现结果，handlers 是服务端实现；
# description 中的 readOnly/destructive 目前只是教学标注，尚未接入真正的权限拦截。
def _mock_server_docs():
    client = MCPClient("docs")
    client.register(
        tool_defs=[
            {"name": "search", "description": "Search documentation. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"}},
                             "required": ["query"]}},
            {"name": "get_version", "description": "Get API version. (readOnly)",
             "inputSchema": {"type": "object", "properties": {},
                             "required": []}},
        ],
        handlers={
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        })
    return client


def _mock_server_deploy():
    client = MCPClient("deploy")
    client.register(
        tool_defs=[
            {"name": "trigger",
             "description": "Trigger a deployment. (destructive — requires approval in real CC)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
            {"name": "status", "description": "Check deployment status. (readOnly)",
             "inputSchema": {"type": "object",
                             "properties": {"service": {"type": "string"}},
                             "required": ["service"]}},
        ],
        handlers={
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        })
    return client


# 教学版的“Server 配置中心”：传入的名称先在这里解析成对应的连接工厂。
# 真实系统通常还要合并用户、项目、插件、企业策略等多层 MCP 配置。
MOCK_SERVERS = {
    "docs": _mock_server_docs,
    "deploy": _mock_server_deploy,
}


# connect_mcp 本身仍是一个内置工具：模型必须先有能力调用它，才能发现外部工具。
# 该函数只完成“连接 + 注册发现结果”；它不会直接修改当前 agent_loop 手中的 tools。
# 真正让新工具生效的是本轮工具执行结束后，再次调用 assemble_tool_pool。
def connect_mcp(name: str) -> str:
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"
    factory = MOCK_SERVERS.get(name)
    if not factory:
        available = ", ".join(MOCK_SERVERS.keys())
        return f"Unknown server '{name}'. Available: {available}"
    # factory() 在教学版中立即构造客户端并填充工具列表；真实连接通常会在此
    # 启动子进程或建立远程连接，再发送 tools/list 请求。
    mcp_client = factory()
    mcp_clients[name] = mcp_client
    tool_names = [t["name"] for t in mcp_client.tools]
    print(f"  \033[31m[mcp] connected: {name} → {tool_names}\033[0m")
    return (f"Connected to MCP server '{name}'. "
            f"Discovered {len(mcp_client.tools)} tools: {', '.join(tool_names)}")


# 这是 s19 的核心适配层：把来源不同的工具统一转换成 Anthropic tools + handlers。
# 返回的两个集合按同一个 name 建立对应关系：模型看到 tools 中的 schema，执行阶段
# 再用同名 key 从 handlers 中找到 Python 调用入口。
def assemble_tool_pool() -> tuple[list[dict], dict]:
    """Assemble builtin tools + all MCP tools into one pool."""
    # 必须复制而不是直接修改全局 BUILTIN_*，否则每次重建都会重复追加 MCP 工具。
    tools = list(BUILTIN_TOOLS)
    handlers = dict(BUILTIN_HANDLERS)
    for server_name, mcp_client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in mcp_client.tools:
            safe_tool = normalize_mcp_name(tool_def["name"])
            # 不同 Server 都可能提供 search/status 等同名工具；双层前缀把来源编码进
            # 模型可见名称，避免覆盖内置工具或其他 Server 的工具。
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            # MCP 定义使用 inputSchema；传给 Anthropic API 时转换为 input_schema。
            # description 原样保留，因此 readOnly/destructive 标注也会展示给模型。
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": tool_def.get("inputSchema", {}),
            })
            # c、t 通过默认参数绑定当前循环值，避免 Python 闭包的 late binding：
            # 若直接引用循环变量，所有 lambda 最终可能都调用最后一个 Server/工具。
            handlers[prefixed] = (
                lambda *, c=mcp_client, t=tool_def["name"], **kw: c.call_tool(t, kw))
    return tools, handlers


# ═══════════════════════════════════════════════════════════
# FROM s18 (unchanged): Lead 工具适配函数
# ═══════════════════════════════════════════════════════════

def run_create_worktree(name: str, task_id: str = "") -> str:
    return create_worktree(name, task_id)

def run_remove_worktree(name: str, discard_changes: bool = False) -> str:
    return remove_worktree(name, discard_changes)

def run_keep_worktree(name: str) -> str:
    return keep_worktree(name)


# ═══════════════════════════════════════════════════════════
# FROM s12-s18 + NEW in s19: 内置工具 handler 适配层
# ═══════════════════════════════════════════════════════════
# 大多数 wrapper 沿用此前章节；run_connect_mcp 将新的连接能力纳入内置 handler 表。

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks)


def run_get_task(task_id: str) -> str:
    return get_task_json(task_id)

def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")

def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)

def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"Sent to {to}"

def run_check_inbox() -> str:
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        meta = m.get("metadata", {})
        req_id = meta.get("request_id", "")
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return "\n".join(lines)

def run_connect_mcp(name: str) -> str:
    return connect_mcp(name)


# ═══════════════════════════════════════════════════════════
# FROM s01-s18 + NEW in s19: Lead 内置工具定义
# ═══════════════════════════════════════════════════════════
# BUILTIN_TOOLS 仍是启动时必定存在的基础能力。s19 只新增 connect_mcp；真正的
# mcp__docs__* / mcp__deploy__* 不静态写在这里，而由 assemble_tool_pool 动态生成。

BUILTIN_TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "create_task", "description": "Create a task.",
     "input_schema": {"type": "object",
                      "properties": {"subject": {"type": "string"},
                                     "description": {"type": "string"},
                                     "blockedBy": {"type": "array",
                                                   "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "get_task", "description": "Get full task details.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task", "description": "Claim a pending task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task", "description": "Complete an in-progress task.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "spawn_teammate", "description": "Spawn an autonomous teammate.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "role": {"type": "string"},
                                     "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message", "description": "Send message to a teammate.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check inbox for messages and protocol responses.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "request_shutdown",
     "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"}},
                      "required": ["teammate"]}},
    {"name": "request_plan",
     "description": "Ask a teammate to submit a plan.",
     "input_schema": {"type": "object",
                      "properties": {"teammate": {"type": "string"},
                                     "task": {"type": "string"}},
                      "required": ["teammate", "task"]}},
    {"name": "review_plan",
     "description": "Approve or reject a submitted plan.",
     "input_schema": {"type": "object",
                      "properties": {"request_id": {"type": "string"},
                                     "approve": {"type": "boolean"},
                                     "feedback": {"type": "string"}},
                      "required": ["request_id", "approve"]}},
    {"name": "create_worktree",
     "description": "Create an isolated git worktree.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "task_id": {"type": "string"}},
                      "required": ["name"]}},
    {"name": "remove_worktree",
     "description": "Remove a worktree. Refuses if changes exist.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"},
                                     "discard_changes": {"type": "boolean"}},
                      "required": ["name"]}},
    {"name": "keep_worktree",
     "description": "Keep a worktree for manual review.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
    # MCP 的“入口工具”必须是 builtin，否则尚未连接 Server 时模型无从发起连接。
    {"name": "connect_mcp",
     "description": "Connect to an MCP server (docs, deploy) and discover tools.",
     "input_schema": {"type": "object",
                      "properties": {"name": {"type": "string"}},
                      "required": ["name"]}},
]

# schema 与执行逻辑分离：模型只接收 BUILTIN_TOOLS，真正调用时再按名称查 handler。
# assemble_tool_pool 会复制此表，并继续加入动态生成的 MCP handler。
BUILTIN_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message, "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan, "review_plan": run_review_plan,
    "create_worktree": run_create_worktree,
    "remove_worktree": run_remove_worktree,
    "keep_worktree": run_keep_worktree,
    "connect_mcp": run_connect_mcp,
}


# ═══════════════════════════════════════════════════════════
# FROM s09-s18 (unchanged): 轻量上下文更新
# ═══════════════════════════════════════════════════════════

MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"


def update_context(context: dict, messages: list) -> dict:
    memories = ""
    if MEMORY_INDEX.exists():
        memories = MEMORY_INDEX.read_text()[:2000]
    return {"memories": memories}


# ═══════════════════════════════════════════════════════════
# NEW in s19: 使用动态工具池的 Lead Agent Loop
# ═══════════════════════════════════════════════════════════
# 与此前固定 BUILTIN_TOOLS 的循环相比，本章每次进入 agent_loop 都重新组装工具池。
# 原因是 MCP 连接会在对话进行中改变“模型可调用工具集合”，旧缓存中的 tools/system
# 无法表达新状态，因此教学版直接取消 prompt cache，优先保证工具列表正确。

def agent_loop(messages: list, context: dict):
    # 初始时通常只有 builtin；若前一个用户 turn 已连接 Server，这里也会自动带上它们。
    tools, handlers = assemble_tool_pool()
    system = assemble_system_prompt(context)
    while True:
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=tools, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            # 内置工具和 MCP 工具已经被组装为同一张 handlers 表，因此执行阶段
            # 不需要再判断工具来源：mcp__ 前缀工具也走完全相同的分发路径。
            handler = handlers.get(block.name)
            output = handler(**block.input) if handler else "Unknown"
            print(str(output)[:300])
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

        # connect_mcp 在本轮执行中只更新 mcp_clients。必须在下一次请求模型前重建：
        #   tools      —— 让 API 接收到新发现的工具 schema；
        #   handlers   —— 让工具名能路由到对应 MCPClient；
        #   system     —— 让模型看到当前已连接 Server。
        # 若遗漏这一步，连接虽然成功，但当前循环仍拿着旧工具池，模型无法调用新工具。
        if any(b.name == "connect_mcp" for b in response.content
               if b.type == "tool_use"):
            tools, handlers = assemble_tool_pool()
            context = update_context(context, messages)
            system = assemble_system_prompt(context)


# ═══════════════════════════════════════════════════════════
# FROM s01-s18 (unchanged): 终端多轮对话外循环
# ═══════════════════════════════════════════════════════════
# main 负责持续接收用户输入并保存 history；每次 agent_loop 则完成一个用户 turn 内部的
# “模型响应 → 工具调用 → tool_result → 再次模型响应”循环。MCP 连接会跨 turn 保留在
# 全局 mcp_clients 中，直到进程退出。
if __name__ == "__main__":
    print("s19: mcp tools")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = {"memories": ""}
    while True:
        try:
            query = input("\033[36ms19 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))

        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:
            inbox_text = "\n".join(
                f"From {m['from']} [{m.get('type', 'message')}]: "
                f"{m['content'][:200]}" for m in inbox)
            history.append({"role": "user",
                            "content": f"[Inbox]\n{inbox_text}"})
        print()
