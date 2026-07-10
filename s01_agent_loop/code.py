#!/usr/bin/env python3
# 这一行叫 shebang：在 Linux/macOS 中直接执行该文件时，
# 系统会使用当前环境里的 python3 解释器运行它。

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

# 上面的模块说明给出了本章最核心的 Agent Loop：
# 1. 把 messages 和工具定义交给大模型；
# 2. 大模型决定是否发起 tool_use；
# 3. Harness 真正执行工具；
# 4. 把 tool_result 追加回 messages；
# 5. 再次询问模型，直到模型不再调用工具。
#
# 大模型负责“决策”，这段 Python 程序负责“执行与循环”。
# 也就是说，模型只会生成工具调用请求，并不会自己运行 shell 命令。

# os：读取环境变量、获取当前工作目录等。
import os
# subprocess：让 Python 可以启动子进程并执行 shell 命令。
import subprocess

# readline 只用于改善终端输入体验，不属于 Agent Loop 的核心逻辑。
# 某些环境可能没有该模块，所以使用 try/except 保证程序仍能运行。
try:
    import readline
    # macOS 的 libedit 在处理中文输入时有退格问题，这四行修复它
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    # 没有 readline 时忽略即可，只会少一些命令行编辑能力。
    pass

# Anthropic 是模型 API 客户端。
from anthropic import Anthropic
# load_dotenv 用来把 .env 文件中的配置加载为环境变量。
from dotenv import load_dotenv

# override=True 表示 .env 中的同名配置可以覆盖当前进程已有的环境变量。
load_dotenv(override=True)

# 如果配置了自定义 API 地址，就删除可能与之冲突的认证变量。
# pop 的第二个参数为 None，表示变量不存在时也不会抛出异常。
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 创建模型客户端；未配置 ANTHROPIC_BASE_URL 时，getenv 会返回 None，
# 客户端将使用默认的 API 地址。
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

# 使用 [] 读取环境变量意味着 MODEL_ID 是必填项；
# 如果没有配置，程序会尽早报错，而不是把 None 传给 API。
MODEL = os.environ["MODEL_ID"]

# SYSTEM 是系统提示词。
# os.getcwd() 把当前工作目录告诉模型，使模型知道应该在哪个项目中操作。
# “Use bash”与下面声明的 bash 工具相对应。
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

# ── Tool definition: just bash ────────────────────────────
# TOOLS 只是“提供给模型看的工具说明”，并不负责真正执行命令。
#
# 模型看到该 JSON Schema 后，就知道自己可以生成类似下面的调用：
# {"name": "bash", "input": {"command": "ls -la"}}
#
# 真正的执行逻辑位于后面的 run_bash()。
TOOLS = [{
    # 工具名称。模型发起调用时会使用这个名字。
    "name": "bash",
    # 工具用途描述，帮助模型判断什么时候应该调用它。
    "description": "Run a shell command.",
    # input_schema 规定模型传入参数的结构。
    "input_schema": {
        # 输入必须是一个 JSON 对象。
        "type": "object",
        # 对象中有一个字符串类型的 command 字段。
        "properties": {"command": {"type": "string"}},
        # required 表示 command 不能省略。
        "required": ["command"],
    },
}]


# ── Tool execution ────────────────────────────────────────
# 这是 bash 工具的真实实现。
# command: str 表示参数预期为字符串，-> str 表示返回字符串。
def run_bash(command: str) -> str:
    # 教学版使用一个简单的危险命令黑名单。
    # 它只能演示“执行前检查”这一概念，不能替代生产级沙箱和权限系统。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]

    # 生成器表达式逐项检查危险片段是否出现在命令中；
    # any(...) 只要发现一个 True 就会阻止执行。
    if any(d in command for d in dangerous):
        # 返回字符串而不是抛异常，模型可以看到失败原因并调整下一步。
        return "Error: Dangerous command blocked"
    try:
        # subprocess.run 会真正启动 shell 子进程：
        # - shell=True：支持管道、重定向、&& 等 shell 语法；
        # - cwd=os.getcwd()：在当前项目目录执行；
        # - capture_output=True：捕获 stdout 和 stderr；
        # - text=True：把输出作为字符串返回；
        # - timeout=120：最多执行 120 秒。
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)

        # 将正常输出和错误输出合并后反馈给模型。
        out = (r.stdout + r.stderr).strip()

        # 最多返回 50000 个字符，避免工具输出无限占用模型上下文；
        # 没有输出时显式返回 "(no output)"。
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        # 命令超时也转换为普通 tool_result，让模型能够继续处理。
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        # 捕获常见系统错误，避免一次工具失败导致整个 Agent 进程退出。
        return f"Error: {e}"


# ── The core pattern: a while loop that calls tools until the model stops ──
# messages 是累计消息列表，里面会依次保存：
# 用户问题 → 模型回答/工具调用 → 工具结果 → 模型后续回答……
#
# 这个函数对应页面中的内层循环：
# 模型调用工具就继续，不调用工具就结束。
def agent_loop(messages: list):
    # while True 表示持续运行；真正的退出条件在 stop_reason 判断处。
    while True:
        # 第 2 步：把完整消息历史和工具定义一起发给模型。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )

        # response.content 是模型这一轮产生的内容块列表，
        # 其中既可能有普通文本块，也可能有 tool_use 块。

        # Append assistant turn
        # 第 3 步：先把模型本轮回答原样追加到历史。
        # 下一轮调用模型时，它必须能看到自己刚才发起了哪些工具调用。
        messages.append({"role": "assistant", "content": response.content})

        # If the model didn't call a tool, we're done
        # stop_reason == "tool_use" 表示模型“举手说要使用工具”；
        # 不等于 tool_use 表示模型已经给出了最终回答，循环结束。
        if response.stop_reason != "tool_use":
            return

        # Execute each tool call, collect results
        # 第 4 步：执行模型请求的所有工具，并收集对应结果。
        # 一次 response 中可能包含不止一个工具调用，所以使用列表。
        results = []

        # response.content 中可能混有文本块，因此逐块判断类型。
        for block in response.content:
            if block.type == "tool_use":
                # block.input 是模型生成的工具参数。
                # ANSI 转义序列把即将执行的命令显示为黄色。
                print(f"\033[33m$ {block.input['command']}\033[0m")

                # 模型只负责提出命令；真正执行发生在这里。
                output = run_bash(block.input["command"])

                # 终端只预览前 200 个字符，
                # 但下面反馈给模型的 content 仍是完整 output。
                print(output[:200])

                # 按 Anthropic 工具协议组织 tool_result。
                results.append({
                    "type": "tool_result",
                    # block.id 是本次 tool_use 的唯一标识。
                    # tool_use_id 让模型知道这个结果对应哪一次调用。
                    "tool_use_id": block.id,
                    "content": output,
                })

        # Feed tool results back, loop continues
        # 第 5 步：把工具结果作为新的消息追加回 messages。
        #
        # 这里的 role 是 "user"，不是说真人又输入了一次，
        # 而是 Anthropic Messages API 规定 tool_result 放在 user 消息中。
        messages.append({"role": "user", "content": results})

        # 函数运行到这里后自动回到 while True 顶部：
        # 模型会看到刚才的真实工具结果，并决定继续调用工具还是结束。


# ── Entry point ──────────────────────────────────────────
# 只有直接运行该文件时才进入命令行交互；
# 如果该文件被其他模块 import，这部分不会自动执行。
if __name__ == "__main__":
    print("s01: Agent Loop")
    print("输入问题，回车发送。输入 q 退出。\n")

    # history 保存整个会话历史，因此后续问题也能继承前面的上下文。
    history = []

    # 这是外层循环：负责持续接收真人用户的新任务。
    # agent_loop 内部的 while True 则是模型与工具之间的循环。
    while True:
        try:
            # ANSI 转义序列把输入提示符显示为青色。
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            # Ctrl+D（输入结束）或 Ctrl+C（中断）时退出程序。
            break

        # strip 去除首尾空白，lower 忽略英文大小写；
        # 输入 q、exit 或空字符串都会退出。
        if query.strip().lower() in ("q", "exit", ""):
            break

        # 第 1 步：把真人用户问题追加为一条 user 消息。
        history.append({"role": "user", "content": query})

        # 启动内层 Agent Loop。
        # history 是可变列表，函数内部 append 的内容会直接保留下来。
        agent_loop(history)

        # Print the model's final text response
        # agent_loop 退出后，history 最后一项通常是模型的最终回答。
        response_content = history[-1]["content"]

        # Anthropic 的 content 通常是内容块列表，而不是普通字符串。
        if isinstance(response_content, list):
            for block in response_content:
                # getattr(..., None) 可避免不存在 type 属性时抛异常；
                # 这里只输出最终回答中的普通文本块。
                if getattr(block, "type", None) == "text":
                    print(block.text)

        # 用空行分隔两次命令行任务。
        print()
