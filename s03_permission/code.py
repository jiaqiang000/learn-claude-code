#!/usr/bin/env python3
"""
s03_permission.py - Permission System

Three gates inserted before tool execution:

    Gate 1: Hard deny list (rm -rf /, sudo, ...)
    Gate 2: Rule matching (write outside workspace? destructive cmd?)
    Gate 3: User approval (pause and wait for confirmation)

    +-------+    +--------+    +--------+    +--------+    +------+
    | Tool  | -> | Gate 1 | -> | Gate 2 | -> | Gate 3 | -> | Exec |
    | call  |    | deny?  |    | match? |    | allow? |    |      |
    +-------+    +--------+    +--------+    +--------+    +------+
         |            |             |             |
         v            v             v             v
      (normal)     (blocked)    (ask user)   (user says no?)

Only one line added to the agent loop:

    if not check_permission(block):
        continue

Builds on s02 (multi-tool). Usage:

    python s03_permission/code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import os, subprocess
from pathlib import Path

# readline 只负责改善终端输入体验；部分平台没有该模块，因此导入失败时直接跳过。
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

# 加载模型相关环境变量。使用自定义网关时移除 AUTH_TOKEN，避免与 API Key 鉴权冲突。
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 工作区在程序启动时确定，后面的路径检查和命令执行都以此目录为边界。
WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. All destructive operations require user approval."


# ═══════════════════════════════════════════════════════════
#  继承自 s02（逻辑不变）：工具的具体实现
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # resolve() 会消解 ../ 和符号链接，再判断最终路径是否仍位于工作区内。
    # 这是文件工具自身的底线保护；s03 的权限管线则负责“执行前是否需要批准”。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    # shell=True 允许执行完整 Shell 命令，也意味着 bash 的能力范围很大，
    # 所以不能像文件工具一样只靠 safe_path，必须在执行前经过权限检查。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 限制返回给模型的文本长度，避免超长终端输出占满上下文。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        # limit 只截断返回内容，不会改变磁盘中的原文件。
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
        # 只替换第一次出现的位置，避免同一段文本在多处出现时被全部改掉。
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            # 即使 glob 模式匹配到异常路径，也只返回工作区内部的结果。
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  继承自 s02（逻辑不变）：工具声明与分发
# ═══════════════════════════════════════════════════════════

# TOOLS 是提供给模型的工具说明书：模型根据名称、描述和输入 Schema 生成 tool_use。
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

# 模型只会返回工具名称和参数；该映射负责找到真正执行任务的 Python 函数。
TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# ═══════════════════════════════════════════════════════════
#  s03 新增：三道闸门组成的权限管线
# ═══════════════════════════════════════════════════════════

# 闸门 1：硬拒绝列表。命中后不询问用户，直接禁止执行。
# 这里采用简单的字符串包含判断，便于教学理解；生产系统还需处理命令变体和 Shell 展开等绕过方式。
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]

def check_deny_list(command: str) -> str | None:
    # 返回字符串表示命中及其原因；返回 None 表示可以继续进入下一道闸门。
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


# 闸门 2：规则匹配。这里描述的不是“必定拒绝”，而是“哪些情况必须询问用户”。
# 每条规则包含适用工具、检查函数和展示给用户的原因，后续可以继续追加新规则。
PERMISSION_RULES = [
    {"tools": ["write_file", "edit_file"],
     # 写入路径最终落在工作区外时，需要人工确认。
     "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
     "message": "Writing outside workspace"},
    {"tools": ["bash"],
     # rm、写入 /etc、开放 777 权限等命令具有破坏性或高风险，需要人工确认。
     "check": lambda args: any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
     "message": "Potentially destructive command"},
]

def check_rules(tool_name: str, args: dict) -> str | None:
    # 按定义顺序检查，首次命中就返回原因；权限决策由下一道闸门完成。
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# 闸门 3：仅在规则命中后暂停执行，把工具名、参数和风险原因展示给用户。
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    print(f"\n\033[33m⚠  {reason}\033[0m")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    # 默认值是 N：只有明确输入 y/yes 才放行，其余输入统一视为拒绝。
    return "allow" if choice in ("y", "yes") else "deny"


# 统一权限入口：严格按照“硬拒绝 → 规则匹配 → 用户审批”的顺序执行。
def check_permission(block) -> bool:
    # 硬拒绝表目前只检查 bash 命令；命中后立即结束，不允许通过人工审批绕过。
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"\n\033[31m⛔ {reason}\033[0m")
            return False

    # 未被硬拒绝的调用再进行上下文规则匹配，命中规则后才询问用户。
    reason = check_rules(block.name, block.input)
    if reason:
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False

    # 没有命中任何规则，或用户明确批准，才允许真正执行工具。
    return True


# ═══════════════════════════════════════════════════════════
#  agent_loop：沿用 s02，只在工具执行前插入 check_permission()
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 无论本轮返回文本还是工具调用，都先保存到对话历史，供后续轮次继续推理。
        messages.append({"role": "assistant", "content": response.content})

        # stop_reason 不是 tool_use，说明模型已经给出最终文本，本轮 Agent 循环结束。
        if response.stop_reason != "tool_use":
            return

        # 一次响应可能包含多个 tool_use，因此逐个做权限判断和执行。
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            print(f"\033[36m> {block.name}\033[0m")

            # s03 的核心改动：任何工具都不能绕过该入口直接执行。
            if not check_permission(block):
                # 即使拒绝执行，也必须返回对应 tool_result，让模型知道调用结果而不是一直等待。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "Permission denied."})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        # 工具结果以 user 消息形式回传，模型会据此决定继续调用工具还是输出最终答案。
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s03: Permission")
    print("输入问题，回车发送。输入 q 退出。\n")

    # history 在多轮输入间复用，使模型能够看到此前的请求、调用和工具结果。
    history = []
    while True:
        try:
            query = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
