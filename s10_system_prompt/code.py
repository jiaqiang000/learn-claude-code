#!/usr/bin/env python3
"""
s10: System Prompt — Runtime prompt assembly with caching.

Run:  python s10_system_prompt/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s09:
  - PROMPT_SECTIONS: topic-keyed dict of prompt fragments
  - assemble_system_prompt(context): select + join sections by real state
  - get_system_prompt(context): deterministic cache via json.dumps
  - agent_loop uses get_system_prompt(context) instead of hardcoded SYSTEM

Memory section loads when .memory/MEMORY.md exists (real state, not keywords).
"""

import os, subprocess, json
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# 先读取 .env，再允许其中的值覆盖当前进程环境变量，方便切换教学环境。
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 本章所有路径都以程序启动时的当前目录为根；workspace section 也由它生成。
WORKDIR = Path.cwd()
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


# ── Prompt Sections ──

# 这里只定义各主题的候选片段，还不是最终发送给模型的完整 system prompt。
# 分段后，各能力可以独立维护；组装阶段再决定本轮实际加载哪些片段。
PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file.",
    "workspace": f"Working directory: {WORKDIR}",
    # memory 正文会在组装时从 context 动态注入；这里保留的是该 section 的说明性模板。
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    """Select and join prompt sections based on current context."""
    # sections 的追加顺序就是最终 prompt 的顺序。保持顺序稳定，也更有利于真实系统命中前缀缓存。
    sections = []

    # Always loaded — identity, tools, workspace
    # 这三段描述每次调用都需要的基本运行约束，因此不依赖用户本轮说了什么。
    # 教学版的 tools 文本仍是固定字符串；真实系统通常会根据 enabled_tools 动态生成。
    sections.append(PROMPT_SECTIONS["identity"])
    sections.append(PROMPT_SECTIONS["tools"])
    sections.append(PROMPT_SECTIONS["workspace"])

    # Conditional — memory loaded when MEMORY.md exists and has content
    # 是否加载 memory 只看 update_context 得到的真实文件状态，不在用户消息中猜关键词。
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")

    # 双换行把各 section 隔开，最终仍作为一个完整字符串传给 Anthropic API。
    return "\n\n".join(sections)


# 这里只缓存最近一次组装结果，并不是可保存多个 context 的通用缓存表。
_last_context_key = None
_last_prompt = None


def get_system_prompt(context: dict) -> str:
    """Cache wrapper — reassemble only when context changes.

    Uses json.dumps for deterministic serialization, not Python's hash()
    which has process randomization and fails on nested dicts/lists.
    This cache only avoids redundant string assembly within a process.
    Real Claude Code additionally protects API-level prompt cache via
    stable section ordering and SYSTEM_PROMPT_DYNAMIC_BOUNDARY.
    """
    global _last_context_key, _last_prompt
    # 把嵌套 dict/list 确定性序列化：键排序保证同一状态得到同一字符串。
    # default=str 让 Path 等非 JSON 原生对象也能参与生成缓存键。
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    # context 未变化时直接复用字符串，省掉重复选择和拼接 section 的工作。
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    # 只要工具、工作目录或记忆内容等任一状态变化，就重新组装并覆盖旧缓存。
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    # loaded 仅用于展示本次加载了哪些 section，不参与 prompt 本身的生成。
    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


# ── Tools ──

def safe_path(p: str) -> Path:
    # resolve 会消解 ../ 和符号链接，再检查结果是否仍位于工作目录中。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    try:
        # 子进程固定在 WORKDIR 执行；超时和输出上限避免单次工具结果无限占用上下文。
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        # limit 是按行截断，而不是一次把超长文件完整返回给模型。
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path)
        # 写入嵌套路径时自动创建父目录，例如工具可直接创建 .memory/MEMORY.md。
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# TOOLS 是提供给模型的工具协议；它决定模型能看到哪些工具名称和参数结构。
TOOLS = [
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
]

# TOOL_HANDLERS 是本地执行注册表。模型返回 tool_use 后，通过同名 key 找到真正的 Python 函数。
TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state: which tools exist, whether memory files exist."""
    # 当前教学版没有使用传入的旧 context/messages，而是每次重新读取运行环境，生成一份状态快照。
    memories = ""
    # 这里只读取记忆索引；文件不存在或内容为空时，memory section 不会进入 prompt。
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        # 从实际注册表取工具名，而不是在消息中判断用户是否可能需要某个工具。
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    """Main loop — uses assembled system prompt instead of hardcoded SYSTEM."""
    # 进入本轮 Agent 循环时，先根据当前真实状态取得 prompt；可能新组装，也可能命中缓存。
    system = get_system_prompt(context)
    while True:
        response = client.messages.create(
            model=MODEL, system=system, messages=messages,
            tools=TOOLS, max_tokens=8000)
        # 无论响应是最终文本还是工具请求，都先原样写入 history，保持 API 消息链完整。
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        # 一次模型响应可能同时包含多个 tool_use block，统一执行后再批量回传结果。
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            # 工具协议中的 name 与本地处理函数通过注册表解耦。
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        # Anthropic 消息协议要求 tool_result 以 user 消息身份跟在对应的 assistant tool_use 后。
        messages.append({"role": "user", "content": results})

        # Re-evaluate context and prompt after each tool round
        # 这是本章最关键的时机：例如 write_file 刚创建 MEMORY.md，下一次模型调用就应加载 memory。
        context = update_context(context, messages)
        system = get_system_prompt(context)


if __name__ == "__main__":
    print("s10: system prompt — runtime assembly")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    # history 跨多轮用户输入持续保留；context 则在每轮/每次工具执行后重新从真实状态计算。
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms10 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        # agent_loop 内部更新的是其局部 context，因此返回主交互循环后再同步一次最新状态。
        context = update_context(context, history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
