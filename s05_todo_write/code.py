#!/usr/bin/env python3
"""
s05: TodoWrite — add a planning tool on top of s04 hooks.

  +---------+      +-------+      +------------------+
  |  User   | ---> |  LLM  | ---> | TOOL_HANDLERS    |
  | prompt  |      |       |      |  bash            |
  +---------+      +---+---+      |  read_file       |
                        ^         |  write_file      |
                        | result  |  edit_file       |
                        +---------+  glob            |
                                      todo_write ← NEW
                                   +------------------+
                                        |
                         in-memory current_todos
                                        |
                        if rounds_since_todo >= 3:
                          inject <reminder>

Changes from s04:
  + todo_write tool + run_todo_write() implementation
  + Nag reminder (inject reminder after 3 rounds without todo update)
  + SYSTEM prompt includes "plan before execute" guidance
  + rounds_since_todo counter in agent_loop
  Loop unchanged: new tool auto-dispatches via TOOL_HANDLERS.

Run: python s05_todo_write/code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import ast, json, os, subprocess
from pathlib import Path

# readline 只用于改善交互式终端的输入体验；某些平台没有该模块，缺失时不影响 Agent 主流程。
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# 从 .env 覆盖加载模型配置；使用自定义 API 地址时，移除可能与之冲突的 AUTH_TOKEN。
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# WORKDIR 同时是 Shell 命令的执行目录和文件工具允许访问的工作区边界。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# TodoWrite 的任务状态只保存在当前 Python 进程内存中：程序退出后会清空，也没有跨 Agent 的共享与并发控制。
CURRENT_TODOS: list[dict] = []

# s05 change: SYSTEM prompt adds planning guidance
# SYSTEM 只是向模型提出“先规划、再更新状态”的行为要求；真正可调用的规划入口仍由 todo_write 工具提供。
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Before starting any multi-step task, use todo_write to plan your steps. "
    "Update status as you go."
)


# ═══════════════════════════════════════════════════════════
#  FROM s02-s04 (unchanged): Tool Implementations
# ═══════════════════════════════════════════════════════════

# 所有文件类工具都先经过 safe_path：消解 ../ 和符号链接后，再确认最终路径没有逃出工作区。
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # shell=True 允许模型使用管道、重定向等完整 Shell 语法，因此执行前仍要经过 PreToolUse 权限 Hook。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 仅截断返回给模型的文本，命令本身已经完整执行。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        # limit 控制本次给模型的行数，不会修改磁盘中的源文件。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        # 只替换首次出现的位置，避免相同文本在多个位置被一次性全部修改。
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            # glob 负责匹配，这里再次过滤最终路径，避免把工作区外的结果返回给模型。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s05: todo_write tool — plan only, no execution
# ═══════════════════════════════════════════════════════════

# 统一整理并校验 todo_write 的入参。
# 正常工具调用应传入 list；同时兼容某些模型或网关把整个数组序列化成字符串的情况。
def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            # 优先按标准 JSON 解析，例如 '[{"content": "读代码", "status": "pending"}]'。
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                # literal_eval 兼容 Python 字面量形式，但不会像 eval 那样执行任意代码。
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"

    # 工具 schema 会提前约束模型输出，这里的检查仍然必要：运行时不能假设输入一定完全合规。
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None


# TodoWrite 不执行任务，只把模型提交的“完整任务列表”替换进内存，并把当前进度打印到终端。
# 因此状态更新不是修改某一项，而是模型每次重新提交整张列表。
def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    todos, error = _normalize_todos(todos)
    if error:
        return error

    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        # ANSI 转义码只负责终端颜色；三种状态本质上仍是普通字符串。
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    # 返回值会作为 tool_result 写回对话，让模型知道本次列表更新已经成功。
    return f"Updated {len(CURRENT_TODOS)} tasks"


# TOOLS 是发送给模型的工具说明书：定义可选工具名、参数结构和必填字段，并不直接绑定 Python 函数。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    # s05: new tool
    # 与其他工具不同，todo_write 的参数是带状态的任务数组；它给 Agent 增加规划能力，而不是新的文件或命令执行能力。
    {"name": "todo_write", "description": "Create and manage a task list for your current coding session.",
     "input_schema": {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["content", "status"]}}}, "required": ["todos"]}},
]

# TOOL_HANDLERS 才是本地运行时分发表。新增工具只要加入映射，后面的通用 dispatch 代码无需为它单独写分支。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob, "todo_write": run_todo_write,
}


# ═══════════════════════════════════════════════════════════
#  FROM s04 (unchanged): Hook System
# ═══════════════════════════════════════════════════════════

# Hook 注册表继承自 s04；s05 的 TodoWrite 没有改变 Hook 的事件和分发方式。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        # 任一 Hook 返回非 None 就短路：PreToolUse 可据此阻止工具，Stop 可据此要求循环继续。
        if result is not None:
            return result
    return None


# s04 hooks preserved
# 这是教学版的字符串拒绝列表，只覆盖明显危险命令，不等同于生产环境的完整 Shell 安全分析。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]


def permission_hook(block):
    """PreToolUse: deny list check."""
    if block.name == "bash":
        for p in DENY_LIST:
            if p in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{p}'\033[0m")
                return "Permission denied"
    return None


def log_hook(block):
    """PreToolUse: log tool calls."""
    print(f"\033[90m[HOOK] {block.name}\033[0m")
    return None


def context_inject_hook(query: str):
    """UserPromptSubmit: log working directory."""
    # 当前实现只是记录工作目录，并没有把额外文本真正拼接进 query。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


def summary_hook(messages: list):
    """Stop: print tool call count."""
    # 工具结果以 role=user 的内容块写回 messages；这里遍历历史统计实际产生过多少个 tool_result。
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None


register_hook("UserPromptSubmit", context_inject_hook)
# permission_hook 先注册，因此命中拒绝项时 trigger_hooks 会直接短路，后面的 log_hook 不再执行。
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — same as s04 + nag reminder counter
# ═══════════════════════════════════════════════════════════

# 该计数器统计“连续多少轮模型工具调用没有更新 Todo”，不是工具总数，也不是用户提问次数。
rounds_since_todo = 0


def agent_loop(messages: list):
    global rounds_since_todo
    while True:
        # s05: nag reminder — inject if model hasn't updated todos for 3 rounds
        # reminder 作为一条新的 user 消息写入历史，并在下一次请求模型前生效；它是提示，不会强制执行 todo_write。
        if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 先保存完整 assistant 响应，之后执行其中的 tool_use，并把对应 tool_result 再写回历史。
        messages.append({"role": "assistant", "content": response.content})

        # 模型不再请求工具时，本轮 Agent 任务结束；Todo 列表本身仍保留在当前进程内存中。
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 一次 LLM 响应即算一轮，即使 response.content 中同时包含多个 tool_use block，也只先加 1。
        rounds_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # PreToolUse 在真正 dispatch 前运行；被拦截时仍需返回 tool_result，否则模型会一直等待该调用结果。
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # 通用分发逻辑与 s04 相同，todo_write 通过 TOOL_HANDLERS 自动接入，不需要额外 if/elif。
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            trigger_hooks("PostToolUse", block, output)

            # s05: reset nag counter when todo_write is called
            # 同一响应里只要执行到 todo_write 的 dispatch，连续未更新轮数就清零；即使参数校验返回错误也会重置。
            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": output})

        # Anthropic 工具协议要求把所有结果作为下一条 user 消息返回，模型才能基于执行结果继续推理。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s05: TodoWrite — plan before execute, nag if you forget")
    print("Type a question, press Enter. Type q to quit.\n")

    # history 在整个交互进程中持续复用，所以多轮用户输入共享同一份对话上下文和 CURRENT_TODOS。
    history = []
    while True:
        try:
            query = input("\033[36ms05 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)

        # agent_loop 返回时，history 最后一项应是模型的最终 assistant 响应；这里只打印其中的文本块。
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
