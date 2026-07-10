#!/usr/bin/env python3
"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.

Usage:
    pip install anthropic python-dotenv
    ANTHROPIC_API_KEY=... python s01_agent_loop/code.py
"""

import os
import subprocess

# readline 仅用于改善终端输入体验，不属于 Agent Loop 的核心逻辑。
try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

# 从 .env 加载 API 地址、模型 ID、密钥等配置。
load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 把当前工作目录告诉模型，让它知道 bash 命令应操作哪个项目。
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# ── Tool definition: just bash ────────────────────────────
# 这里只是向模型声明“有一个 bash 工具可以调用”。
# 工具的真实执行逻辑在后面的 run_bash() 中。
TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


# ── Tool execution ────────────────────────────────────────
# 执行模型生成的 shell 命令，并把输出作为字符串返回给模型。
# 这里的黑名单只是教学示例，生产环境还需要沙箱、权限控制等保护。
def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        # 限制返回长度，避免一次工具输出占满模型上下文。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ── The core pattern: a while loop that calls tools until the model stops ──
# Agent 的核心循环：
# 1. 把完整消息历史和工具定义发给模型；
# 2. 模型调用工具时，由 Harness 执行并回传结果；
# 3. 模型不再调用工具时，当前任务结束。
def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )

        # 先保存模型本轮回答，下一轮模型才能看到自己发起过哪些工具调用。
        messages.append({"role": "assistant", "content": response.content})

        # stop_reason 不是 tool_use，表示模型已经给出最终回答，退出内层循环。
        if response.stop_reason != "tool_use":
            return

        # 一次回答里可能包含多个 tool_use，因此逐个执行并收集结果。
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m$ {block.input['command']}\033[0m")
                output = run_bash(block.input["command"])
                print(output[:200])
                results.append({
                    "type": "tool_result",
                    # 通过 tool_use_id 将执行结果与对应的工具调用关联起来。
                    "tool_use_id": block.id,
                    "content": output,
                })

        # 按 Anthropic 协议把工具结果放入 user 消息中；
        # 追加后 while 循环会再次调用模型，让它根据真实结果继续决策。
        messages.append({"role": "user", "content": results})


# ── Entry point ──────────────────────────────────────────
if __name__ == "__main__":
    print("s01: Agent Loop")
    print("输入问题，回车发送。输入 q 退出。\n")

    # history 保存整个会话；其中既有真人消息，也有模型回答和工具结果。
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})

        # 外层循环负责接收真人的新问题，agent_loop 是模型与工具的内层循环。
        agent_loop(history)

        # Print the model's final text response
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
        print()
