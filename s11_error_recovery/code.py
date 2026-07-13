#!/usr/bin/env python3
"""
s11: Error Recovery — 错误恢复

本章目标：
  在 s10 的 Agent Loop 外增加恢复层。LLM 调用出现可恢复问题时，不立即结束，
  而是根据问题类型调整请求，再回到同一个循环继续执行。

主要变化：
  1. 输出达到 max_tokens：先把输出预算从 8K 提升到 64K；仍被截断时再保存内容并续写。
  2. prompt/context 过长：紧急压缩 messages 后重试一次。
  3. 429/529：指数退避并加入随机抖动；连续 529 时记录备用模型。
  RecoveryState 统一保存各路径的恢复进度，避免无限重试。

整体流程：
  组装 prompt -> 调用 LLM
    -> 429/529：在 with_retry 内等待后重试
    -> prompt_too_long：压缩 messages，continue 回到循环开头
    -> max_tokens：扩大输出预算或追加续写提示，continue 回到循环开头
    -> 正常 tool_use：执行工具，沿用原有 Agent Loop

Run:  python s11_error_recovery/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY
"""

# ═══════════════════════════════════════════════════════════
#  FROM s10 (unchanged): 基础运行环境与主模型配置
# ═══════════════════════════════════════════════════════════

import os, subprocess, time, random, json
from pathlib import Path

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
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
PRIMARY_MODEL = os.environ["MODEL_ID"]

# ═══════════════════════════════════════════════════════════
#  NEW in s11: 错误恢复配置
# ═══════════════════════════════════════════════════════════

FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# 输出截断路径：第一次扩到 64K；扩容后仍截断，最多再续写 3 次。
ESCALATED_MAX_TOKENS = 64000
DEFAULT_MAX_TOKENS = 8000
MAX_RECOVERY_RETRIES = 3

# 瞬态错误路径：429/529 最多重试 10 次；连续 3 次 529 时尝试使用备用模型。
MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_CONSECUTIVE_529 = 3
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)

# ═══════════════════════════════════════════════════════════
#  FROM s10 (unchanged): 动态 System Prompt 组装与缓存
# ═══════════════════════════════════════════════════════════

# s11 不改 prompt 的生成方式；恢复后仍使用这套组装结果重新调用 LLM。

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    loaded = ["identity", "tools", "workspace"]
    if context.get("memories"):
        loaded.append("memory")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


# ═══════════════════════════════════════════════════════════
#  FROM s10 (unchanged): 工具定义、执行函数与分发表
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
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

TOOL_HANDLERS = {"bash": run_bash, "read_file": run_read, "write_file": run_write}


# ═══════════════════════════════════════════════════════════
#  NEW in s11: 恢复状态与重试辅助函数
# ═══════════════════════════════════════════════════════════

# 这一层只负责“怎样取得一次可继续处理的 LLM 响应”，不会改动后面的工具执行流程。

class RecoveryState:
    """保存一次 agent_loop 调用期间共享的恢复进度。

    has_escalated / recovery_count：记录 max_tokens 扩容与续写；
    has_attempted_reactive_compact：限制紧急压缩只能执行一次；
    consecutive_529 / current_model：记录连续过载次数与当前模型。
    各路径分别计数，使一种错误不会触发无上限的恢复循环。
    """
    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt, retry_after=None):
    """计算下一次重试前的等待时间。

    调用方若传入 retry_after，就优先服从服务端建议；否则使用
    min(500ms × 2^attempt, 32s)，再加 0~25% 随机抖动，避免并发请求同时重试。
    本教学版 with_retry 未解析响应头，因此实际调用走指数退避公式。
    """
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def with_retry(fn, state: RecoveryState):
    """处理不需要修改 messages 的瞬态错误。

    429 和 529 都是在等待后再次执行同一个 fn；成功后清零连续 529 计数。
    其他异常在这里不处理，而是重新抛给 agent_loop 外层判断是否需要压缩上下文。
    """
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()

            # 路径 3A：429 是限流，等待后原样重试请求。
            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429 rate limit] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # 路径 3B：529 是服务过载；除退避外，还累计连续失败次数。
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if FALLBACK_MODEL:
                        # 这里更新的是 state；下方 agent_loop 构造新请求时会读取该模型。
                        # 当前 fn 的 mdl 已在 lambda 创建时固定，本轮剩余重试仍可能使用原模型。
                        state.current_model = FALLBACK_MODEL
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" switching to {FALLBACK_MODEL}\033[0m")
                    else:
                        state.consecutive_529 = 0
                        print(f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                              f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m")
                delay = retry_delay(attempt)
                print(f"  \033[33m[529 overloaded] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # 非瞬态错误不能靠等待解决，交给 agent_loop 外层继续分类。
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    """兼容不同 SDK/网关的报错文本，识别 prompt/context 超限。"""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


def reactive_compact(messages: list) -> list:
    """执行路径 2 的紧急压缩。

    教学版用“裁掉早期历史 + 保留最近 5 条消息”模拟压缩；真实实现会先生成摘要，
    再让摘要与近期消息共同进入下一次 LLM 请求。
    """
    print("  \033[31m[reactive compact] trimming to last 5 messages\033[0m")
    tail = messages[-5:]
    return [{"role": "user",
             "content": "[Reactive compact] Earlier conversation trimmed. "
                        "Continue from where you left off."}, *tail]


# ═══════════════════════════════════════════════════════════
#  FROM s10 (unchanged): 从工作区状态派生 Prompt Context
# ═══════════════════════════════════════════════════════════

def update_context(context: dict, messages: list) -> dict:
    """根据已注册工具、工作目录和记忆文件，重新生成 prompt 所需的 context。"""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": list(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ═══════════════════════════════════════════════════════════
#  NEW in s11: 将三条恢复路径接入 Agent Loop
# ═══════════════════════════════════════════════════════════

def agent_loop(messages: list, context: dict):
    """在原有 Agent Loop 的 LLM 调用周围接入错误恢复。

    内层 with_retry 处理 429/529；外层 try/except 处理上下文超限；
    成功拿到响应后，再根据 stop_reason 处理 max_tokens。
    三条路径恢复成功后都通过 continue 回到 while 开头，正常响应则继续原有工具流程。
    """
    system = get_system_prompt(context)
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # 路径 3 位于最内层：429/529 只需等待，无须修改 messages。
        # 其他异常会从 with_retry 抛出，进入外层 except 继续分类。
        try:
            response = with_retry(
                # 固定本次 with_retry 使用的 token/model 参数，多次重试同一个请求函数。
                lambda mt=max_tokens, mdl=state.current_model:
                    client.messages.create(
                        model=mdl, system=system, messages=messages,
                        tools=TOOLS, max_tokens=mt),
                state)
        except Exception as e:
            # 路径 2：请求内容过长，必须先改变 messages，单纯等待重试没有意义。
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    # 原地替换可让外部 history 同步看到压缩后的消息列表。
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": "[Error] Context too large, cannot continue."}]})
                return

            # 既不是瞬态错误，也不能通过紧急压缩恢复：记录错误并结束本轮。
            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}]})
            return

        # 路径 1：max_tokens 是正常响应的 stop_reason，因此不会进入异常分支。
        if response.stop_reason == "max_tokens":
            # 第一次只扩大输出预算，不把半截回答写入 messages；下一轮仍是同一请求。
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] escalating"
                      f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
                continue
            # 64K 仍被截断时，才保留已生成内容，并用一条续写消息承接它。
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"  \033[33m[max_tokens] continuation"
                      f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
                continue
            print("  \033[31m[max_tokens] recovery limit reached\033[0m")
            return

        # 恢复分支全部结束后，重新接回 s10 已有的正常响应处理。
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        # FROM s10 (unchanged)：执行 tool_use，追加 tool_result，再进入下一轮。
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})

        context = update_context(context, messages)
        system = get_system_prompt(context)


# ═══════════════════════════════════════════════════════════
#  FROM s10 (unchanged): 交互式命令行入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("s11: error recovery")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        turn_start = len(history)
        history.append({"role": "user", "content": query})
        agent_loop(history, context)
        context = update_context(context, history)
        for msg in history[turn_start:]:
            if msg.get("role") != "assistant":
                continue
            for block in msg["content"]:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
