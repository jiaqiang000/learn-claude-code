#!/usr/bin/env python3
"""
s11: Error Recovery — three recovery paths + exponential backoff.

中文导读：
  本章完整保留 s10 的 prompt 组装和工具循环，只在 LLM 调用周围增加恢复层。
  三条路径分别处理“正常响应但输出被截断”“请求因上下文过长失败”
  以及“服务暂时不可用”；它们最终都通过 continue 回到同一个 agent loop。

Run:  python s11_error_recovery/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s10:
  - LLM call wrapped in try/except with three recovery paths
  - Path 1: max_tokens -> escalate 8K->64K (no append on first escalation),
            then continuation prompt (max 3)
  - Path 2: prompt_too_long -> reactive compact -> retry (once)
  - Path 3: 429/529 -> exponential backoff with jitter (max 10),
            fallback model on consecutive 529
  - with_retry wrapper for transient errors
  - RecoveryState tracks escalation / compact / 529 / model

ASCII flow:
  messages -> prompt assembly -> compress+load -> [try] LLM [except] -> tools -> loop
                                                    |          |
                                              stop_reason   error type
                                              max_tokens?   prompt_too_long? -> compact
                                              escalate /    429/529? -> backoff
                                              continue      other? -> log + exit
"""

import os, subprocess, time, random, json
from pathlib import Path

# 不让 readline 自动重绑终端特殊控制字符，使交互快捷键行为保持可预期。
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
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# ── Constants ──

# 输出截断先扩大单次回答预算；只有扩大后仍截断，才进入“保存半截回答并续写”。
ESCALATED_MAX_TOKENS = 64000
DEFAULT_MAX_TOKENS = 8000
# 两套上限互不混用：前者限制 max_tokens 续写次数，后者限制 429/529 请求重试次数。
MAX_RECOVERY_RETRIES = 3
MAX_RETRIES = 10
BASE_DELAY_MS = 500
# 连续过载达到阈值时，尝试把后续请求切到备用模型。
MAX_CONSECUTIVE_529 = 3
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)

# ── Prompt Assembly (from s10, synced) ──

# 这一段沿用 s10：错误恢复包裹的是 LLM 调用，不需要重写 prompt 组装机制。
PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file.",
    "workspace": f"Working directory: {WORKDIR}",
    "memory": "Relevant memories are injected below when available.",
}


def assemble_system_prompt(context: dict) -> str:
    """按当前上下文拼装系统提示词；只有确有记忆时才注入 memory 段。"""
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["workspace"]]
    memories = context.get("memories", "")
    if memories:
        sections.append(f"Relevant memories:\n{memories}")
    return "\n\n".join(sections)


_last_context_key, _last_prompt = None, None


def get_system_prompt(context: dict) -> str:
    """复用最近一次组装结果，避免上下文未变化时重复构造相同 prompt。"""
    global _last_context_key, _last_prompt
    # 将 context 稳定序列化后作为缓存键；sort_keys 避免字典键顺序造成假变化。
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


# ── Tools (unchanged) ──

# 工具层与 s10 保持一致，本章的重点是工具调用前那次 LLM 请求如何恢复。
def safe_path(p: str) -> Path:
    """把相对路径约束在工作目录内，阻止通过 ../ 逃逸到工作区之外。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 限制回传给模型的工具结果大小，避免一次 Bash 输出迅速挤满上下文。
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


# ── Error Recovery (s11 new) ──

class RecoveryState:
    """记录一次 agent_loop 调用期间的恢复进度，防止同一种恢复动作无限循环。"""
    def __init__(self):
        # 路径 1：输出截断时，是否已经升过 token，以及升额后已续写多少次。
        self.has_escalated = False
        self.recovery_count = 0

        # 路径 3 / 路径 2：连续过载次数、是否已做紧急压缩，以及当前模型选择。
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt, retry_after=None):
    """计算指数退避时间：attempt 从 0 开始，基础延迟封顶 32 秒，再加 0~25% 抖动。"""
    # 若调用方解析到了服务端 Retry-After，应以服务端给出的等待时间为准。
    # 当前教学版 with_retry 尚未读取响应头，这个参数是为完整实现预留的。
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    # 抖动可避免许多并发请求在同一时刻醒来，再次形成“重试风暴”。
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def with_retry(fn, state: RecoveryState):
    """只消化 429/529 这类瞬态错误；其他异常原样抛给 agent_loop 外层分类。"""
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            # 一次成功就打断“连续 529”链，下一次过载重新从 1 计数。
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()

            # 429 表示调用频率过高：请求内容不用改，等待后重试即可。
            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)
                print(f"  \033[33m[429 rate limit] retry {attempt+1}/{MAX_RETRIES},"
                      f" wait {delay:.1f}s\033[0m")
                time.sleep(delay)
                continue

            # 529 表示服务过载：同样退避；连续达到阈值时再记录备用模型。
            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if FALLBACK_MODEL:
                        # 这里只更新状态。调用方若已把 mdl 捕获进 fn，本轮剩余重试仍会沿用旧值；
                        # agent_loop 下一次重新构造请求时，才会读取这里记录的备用模型。
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

            # 非瞬态异常不能靠“多等一会儿”解决，交还外层决定压缩还是终止。
            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    """兼容不同 SDK/网关的错误文本，判断是否属于 prompt/context 超限。"""
    msg = str(e).lower()
    return (("prompt" in msg and "long" in msg)
            or "prompt_is_too_long" in msg
            or "context_length_exceeded" in msg
            or "max_context_window" in msg)


def reactive_compact(messages: list) -> list:
    """紧急压缩：教学版仅保留最近 5 条；真实实现会先用 LLM 生成摘要，再携摘要重试。

    它发生在 API 已经拒绝请求之后，因此比 s08 的主动/常规压缩更像最后一道保险。
    """
    print("  \033[31m[reactive compact] trimming to last 5 messages\033[0m")
    # 前置说明消息告诉模型历史被裁剪过；tail 仍保留最近的真实对话与工具结果。
    tail = messages[-5:]
    return [{"role": "user",
             "content": "[Reactive compact] Earlier conversation trimmed. "
                        "Continue from where you left off."}, *tail]


# ── Context ──

def update_context(context: dict, messages: list) -> dict:
    """从当前工具注册表、工作目录和记忆文件重新派生 prompt 所需上下文。"""
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


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    """在原有 Agent Loop 外包一层分类恢复；成功恢复后仍回到同一个 while 循环。"""
    system = get_system_prompt(context)
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # ── LLM call: with_retry handles 429/529, outer handles rest ──
        # 内层 with_retry 负责“等一等再试”；外层 try/except 负责“改变上下文或终止”。
        try:
            response = with_retry(
                # 默认参数固定本次构造请求时的 token/model 值，供 with_retry 多次调用同一 fn。
                lambda mt=max_tokens, mdl=state.current_model:
                    client.messages.create(
                        model=mdl, system=system, messages=messages,
                        tools=TOOLS, max_tokens=mt),
                state)
        except Exception as e:
            # 路径 2：上下文过长时只允许紧急压缩一次，避免“压缩—失败—再压缩”的死循环。
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    # 原地替换列表内容，保证外部 history 仍指向同一个对象。
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                messages.append({"role": "assistant", "content": [
                    {"type": "text",
                     "text": "[Error] Context too large, cannot continue."}]})
                return

            # 其余异常既不属于瞬态错误，也无法通过缩短上下文修复，记录后结束本轮。
            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}]})
            return

        # ── Path 1: max_tokens -> escalate or continue ──
        # max_tokens 是一次“成功响应”的 stop_reason，不会进入上面的异常捕获。
        if response.stop_reason == "max_tokens":
            # 第一次仅扩大输出预算，不保存半截回答；messages 不变，因此重试的仍是原请求。
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] escalating"
                      f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m")
                continue
            # 64K 仍截断：这次保留已生成部分，再追加一条用户续写指令承接原回答。
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(f"  \033[33m[max_tokens] continuation"
                      f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m")
                continue
            print("  \033[31m[max_tokens] recovery limit reached\033[0m")
            return

        # 只有越过 max_tokens 分支，才按正常响应追加，避免首次扩容前污染对话历史。
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        # ── Tool execution ──
        # 模型明确以 tool_use 停止时才执行工具；同一响应中的普通文本块会被跳过。
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

        # 工具可能写入了记忆文件，所以每轮工具执行后重新派生 context 和 system prompt。
        context = update_context(context, messages)
        system = get_system_prompt(context)


if __name__ == "__main__":
    print("s11: error recovery")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    # history 跨用户输入保留；RecoveryState 则在每次 agent_loop 调用时重新创建。
    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 记住本轮起点，执行结束后只打印本轮新增的 assistant 文本。
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
