#!/usr/bin/env python3
"""
s04: Hooks — move extension logic out of the loop, onto hooks.

  User types query
       │
       ▼
  ┌──────────────────┐
  │ UserPromptSubmit │ ── trigger_hooks() before LLM
  └────────┬─────────┘
           ▼
  ┌────────────┐     ┌─────────────────────────────┐
  │  messages  │────▶│  LLM (stop_reason=tool_use?)│
  └────────────┘     │   No ──▶ Stop hooks ──▶ exit │
                     │   Yes ──▶ tool_use block ──┐ │
                     └────────────────────────────┘ │
                                                    ▼
                                          ┌──────────────────┐
                                          │ trigger_hooks()   │
                                          │  PreToolUse:      │
                                          │   permission_hook │
                                          │   log_hook        │
                                          └───────┬──────────┘
                                                  │ (not blocked)
                                          ┌───────▼──────────┐
                                          │ TOOL_HANDLERS[x]  │
                                          └───────┬──────────┘
                                                  │
                                          ┌───────▼──────────┐
                                          │ trigger_hooks()   │
                                          │  PostToolUse:     │
                                          │   large_output    │
                                          └───────┬──────────┘
                                                  │
                                          results ──▶ back to messages

Changes from s03:
  + HOOKS registry (event -> list of callbacks)
  + register_hook() / trigger_hooks()
  + context_inject_hook (UserPromptSubmit)
  + permission_hook, log_hook (PreToolUse)
  + large_output_hook (PostToolUse)
  + summary_hook (Stop)
  - check_permission() removed from loop body
    (logic moved into permission_hook, triggered via PreToolUse)

Run: python s04_hooks/code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import os, subprocess
from pathlib import Path

# readline 只改善终端中的中文输入体验；某些平台没有该模块，因此导入失败时直接跳过。
try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# 从 .env 重新加载模型配置；使用自定义 API 网关时移除可能冲突的 AUTH_TOKEN。
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# WORKDIR 是整个 Agent 的工作区边界：文件路径校验和 Shell 命令都以它为基准。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


# ═══════════════════════════════════════════════════════════
#  继承自 s02-s03（执行逻辑不变）：工具的具体实现
# ═══════════════════════════════════════════════════════════

# 文件工具统一经过 safe_path：先消解 ../ 和符号链接，再检查最终路径是否仍在工作区内。
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # shell=True 能执行完整 Shell 语法，因此真正调用前还要经过 PreToolUse 权限 Hook。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 截断的是返回给模型的文本，不影响命令本身的执行结果。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        # limit 只限制本次返回多少行，磁盘中的原文件不会被修改。
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
        # 只替换第一次出现的位置，避免同一文本在多个位置被意外全部修改。
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            # glob 只负责匹配；这里再次过滤，确保不会把工作区外的路径返回给模型。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"

# TOOLS 是发给模型的“工具说明书”；模型只会据此生成工具名与参数，并不会直接执行 Python 函数。
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
]

# TOOL_HANDLERS 才是运行时分发表：把模型返回的工具名称映射到本地实现。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  s04 新增：Hook 系统（把 s03 的权限逻辑从循环体移到扩展点）
# ═══════════════════════════════════════════════════════════

# 注册表只保存“事件名 -> 回调列表”。Agent 循环不需要知道每个回调的具体业务。
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    # append 保留注册顺序；同一事件的 Hook 会按该顺序依次执行。
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    # *args 让不同事件复用同一个触发器：PreToolUse 传 block，PostToolUse 传 block 和 output。
    for callback in HOOKS[event]:
        result = callback(*args)
        # 教学版把“非 None”作为统一的短路信号：
        # PreToolUse 中表示阻止工具；Stop 中则会被当成续跑提示词。
        if result is not None:  # teaching shortcut: block this tool call
            return result
    return None


# s03 的权限判断本身没有删除，只是包装成 PreToolUse Hook，从而不再侵入 agent_loop。
# 这里仍是便于教学的字符串匹配，生产环境还需考虑命令变体、转义和 Shell 展开。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

# 返回 None 表示允许继续；返回拒绝原因会让 trigger_hooks 立即停止后续 Hook 和工具执行。
def permission_hook(block):
    """PreToolUse: s03 check_permission() logic moved here."""
    if block.name == "bash":
        # 硬拒绝项命中后直接返回，不允许再通过人工确认放行。
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
        # 破坏性关键词不是直接禁止，而是暂停流程并向用户请求明确批准。
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    # 写文件工具也在执行前检查最终路径；工作区外写入必须经过用户确认。
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None

# 日志作为独立 Hook 后，可以随时增加、删除或替换，而无需改 agent_loop。
def log_hook(block):
    """PreToolUse: log every tool call."""
    # 只展示前两个参数值并截断预览，避免日志被大段文件内容撑满。
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None

# PostToolUse 只观察已经产生的输出，不参与工具是否可以执行的权限决策。
def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None

# UserPromptSubmit 在用户消息写入 history、发送给 LLM 之前触发。
# 当前教学实现只打印工作目录；虽然函数名叫 inject，但并没有修改 query。
def context_inject_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

# Stop 在模型不再请求工具、循环准备退出时触发。
def summary_hook(messages: list):
    # tool_result 被作为 role=user 的内容写回 history；这里遍历历史统计实际返回过多少次工具结果。
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    # Stop Hook 返回 None 允许正常退出；若返回字符串，agent_loop 会把它注入为新用户消息并继续。
    return None

# 注册顺序会影响执行顺序。permission_hook 在 log_hook 前面，
# 因而权限 Hook 一旦返回拒绝，trigger_hooks 会短路，后面的日志 Hook 不再执行。
register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop：结构沿用 s03，但循环体不再硬编码具体检查函数
#  s03: if not check_permission(block): ...
#  s04: if trigger_hooks("PreToolUse", block): ...
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    # messages 是可变列表，函数会直接把模型回复和工具结果追加到同一份会话历史中。
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 先保存 assistant 返回的完整内容，后续工具结果才能与对应的 tool_use_id 对齐。
        messages.append({"role": "assistant", "content": response.content})

        # 没有 tool_use 说明模型准备结束；真正 return 前先给 Stop Hook 一次介入机会。
        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            # Stop Hook 返回的字符串被当作新的用户提示词，使模型再运行一轮。
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 一个模型响应中可能同时包含文本块和多个工具调用，只处理 tool_use 块。
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # s04 change: hook replaces hard-coded check_permission()
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                # 拒绝原因仍包装成 tool_result 回传给模型，模型可以据此调整方案，而不是看到调用凭空消失。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            # 只有真正执行过的工具才会走 PostToolUse；被 PreToolUse 拒绝的调用已在上方 continue。
            trigger_hooks("PostToolUse", block, output)  # s04: post hook

            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        # Anthropic 的协议要求工具结果作为下一条 user 消息写回，随后 while 循环再次请求模型。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s04: Hooks — extension logic on hooks, loop stays clean")
    print("Type a question, press Enter. Type q to quit.\n")

    # history 在多次终端输入之间持续复用，因此 Agent 能保留本次会话的上下文。
    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 教学版忽略该 Hook 的返回值，所以这里只能观察输入，尚不能真正改写或拦截 query。
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
