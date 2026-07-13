#!/usr/bin/env python3
# ═════════════════════════════════════════════════════════════════════════════
# s15 章节导读：从“临时子 Agent”走向“可持续通信的 Agent Team”
# ═════════════════════════════════════════════════════════════════════════════
# 本章目标不是改写最内层的 Agent Loop，而是在它外面补齐团队协作所需的
# “队友生命周期 + 异步邮箱 + Lead 唤醒”三件套。核心闭环如下：
#
#   Lead 调用 spawn_teammate
#     → 后台 daemon 线程创建一套独立 messages / tools
#     → 队友通过 MessageBus 把结果追加到 lead.jsonl
#     → inbox_poller 只检测“是否有信”，并向 events 投递 wake
#     → 主线程消费邮箱，把消息作为 user turn 注入 history
#     → 原有 agent_loop 被再次调用，Lead 因而真正“感知并处理”队友结果
#
# s12 的任务图、s13 的后台工具和 s14 的 cron 仍保留给 Lead；本章的主要新增
# 都集中在 MessageBus、spawn_teammate_thread 以及文件末尾的事件合流逻辑。
# 教学版刻意保留三项边界：邮箱没有文件锁、队友最多运行 10 轮、所有线程共享
# WORKDIR；关机握手会在 s16 引入，独立 worktree 则要到 s18 才解决。

"""
s15: Agent Teams — MessageBus + spawn_teammate_thread + inbox injection.

Run:  python s15_agent_teams/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s14:
  - MessageBus class: file-based mailboxes (.mailboxes/*.jsonl)
  - spawn_teammate_thread: creates teammate in background thread
  - Teammate runs own simplified agent_loop (bash, read, write, send_message)
  - Lead tools: spawn_teammate, send_message, check_inbox (3 new)
  - Lead inbox: teammate messages injected into history (not just printed)
  - Teaching version: teammates limited to 10 rounds (real CC uses idle loop)

ASCII flow:
  Lead: cron_queue → messages → prompt → LLM → TOOLS ────→ loop
                ↑                     ↓                        |
                └── inbox ← MessageBus ← teammate.send_message ←┘
  Teammate: inbox → LLM → bash/read/write/send → loop (max 10 turns)
"""

import os, subprocess, json, time, random, threading, queue
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict

# threading 同时承载后台命令、cron、队友和两个输入监听器；queue 则负责把
# 这些异步来源重新串行化，避免多个线程同时修改 Lead 的 history 或调用模型。

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
MODEL = os.environ["MODEL_ID"]

# ═════════════════════════════════════════════════════════════════════════════
# FROM s12-s14 (unchanged): 文件持久化的任务图
# ═════════════════════════════════════════════════════════════════════════════
# 每个任务仍单独落盘到 .tasks。blockedBy 决定依赖是否满足，owner/status 记录
# 认领与生命周期。本章只沿用这套基础设施；教学版队友的精简工具集中并未开放
# Task 工具，因此这里主要仍由 Lead 使用。

TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str          # pending | in_progress | completed
    owner: str | None
    blockedBy: list[str]


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def create_task(subject: str, description: str = "",
                blockedBy: list[str] | None = None) -> Task:
    # 时间戳负责大体有序，四位随机数降低同一秒创建多个任务时的 ID 碰撞概率。
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


def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2)


def can_start(task_id: str) -> bool:
    """Check if all blockedBy dependencies are completed.
    Missing dependencies are treated as blocked."""
    task = load_task(task_id)
    # 依赖文件不存在与依赖尚未完成都算 blocked，避免任务“带病启动”。
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
    if not can_start(task_id):
        deps = [d for d in task.blockedBy
                if not _task_path(d).exists() or load_task(d).status != "completed"]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    # 完成后重新扫描 pending 任务，把本次状态变化解锁的下游任务反馈给模型。
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ═════════════════════════════════════════════════════════════════════════════
# FROM s10-s14 (unchanged): Lead 的动态 System Prompt 组装与缓存
# ═════════════════════════════════════════════════════════════════════════════
# 组装器本身没有因团队机制而改变；变化体现在 tools 文本新增了三种团队工具。
# 队友不会复用这份 prompt，而会在 spawn_teammate_thread 中获得自己的角色说明。

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "get_task, create_task, list_tasks, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron, "
             "spawn_teammate, send_message, check_inbox.",
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
    # 用完整 context 作为缓存键：工作区、工具或记忆没变时复用同一 prompt。
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ═════════════════════════════════════════════════════════════════════════════
# FROM s02-s14 (unchanged): Lead 的基础文件、Shell 与任务工具
# ═════════════════════════════════════════════════════════════════════════════
# 这些 handler 继续负责实际执行；后面的工具 schema 只负责告诉模型“能怎么调”。

def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    # resolve 后再检查父目录，防止 ../ 或符号链接把读写范围带出工作区。
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, run_in_background: bool = False) -> str:
    # run_in_background 只是模型传入的调度意图，是否开线程由 agent_loop 统一判断；
    # 真正执行 Bash 的 handler 保持同步，因而也能被队友的精简循环直接复用。
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
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# Task 工具的这一层 wrapper 负责把内部对象转换成适合回送给模型的字符串。

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●",
                "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} "
                     f"[{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    return claim_task(task_id, owner="agent")


def run_complete_task(task_id: str) -> str:
    return complete_task(task_id)


# ═════════════════════════════════════════════════════════════════════════════
# FROM s13-s14 (unchanged): 后台工具执行与完成通知
# ═════════════════════════════════════════════════════════════════════════════
# 慢命令在线程中执行，完成结果先暂存在共享字典，再包装成 task_notification。
# s15 的新衔接点是：文件末尾的 inbox_poller 也观察 has_pending_background()，
# 让“队友来信”和“后台任务完成”共用同一条 wake → 新 turn 通道。

_bg_counter = 0
background_tasks: dict[str, dict] = {}
background_results: dict[str, str] = {}
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """Fallback heuristic: commands likely to take > 30s."""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """Model explicit request takes priority; fallback to heuristic."""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


def execute_tool(block) -> str:
    """Execute a tool call block, return output."""
    # 这是 Lead 的统一 dispatch map。s15 的三个团队 handler 也挂到这里，
    # 因此主循环无需为每个新增工具增加一套 if/else。
    handler = {
        "bash": run_bash, "read_file": run_read, "write_file": run_write,
        "create_task": run_create_task, "list_tasks": run_list_tasks,
        "get_task": run_get_task, "claim_task": run_claim_task,
        "complete_task": run_complete_task,
        "schedule_cron": run_schedule_cron, "list_crons": run_list_crons,
        "cancel_cron": run_cancel_cron,
        "spawn_teammate": run_spawn_teammate,
        "send_message": run_send_message, "check_inbox": run_check_inbox,
    }.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"


def start_background_task(block) -> str:
    """Run tool in a daemon thread. Returns background task ID."""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    def worker():
        # worker 只写共享状态，不直接改 messages；模型上下文统一由主线程更新。
        result = execute_tool(block)
        with background_lock:
            background_tasks[bg_id]["status"] = "completed"
            background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    threading.Thread(target=worker, daemon=True).start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """Collect completed background results as task_notification messages."""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] == "completed"]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            # pop 表示通知只交付一次；后续重复 wake 不会再次注入同一结果。
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>completed</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        print(f"  \033[32m[background done] {bg_id}: "
              f"{task['command'][:40]} ({len(output)} chars)\033[0m")
    return notifications


def has_pending_background() -> bool:
    """Non-destructive: True if any background task has completed and is
    waiting to be collected. The inbox poller uses this in its wake condition."""
    with background_lock:
        # 与 collect 不同，这里只探测不消费，适合给轮询线程当“门铃”。
        return any(t["status"] == "completed" for t in background_tasks.values())


# ═════════════════════════════════════════════════════════════════════════════
# FROM s14 (unchanged): 持久化 Cron 调度器
# ═════════════════════════════════════════════════════════════════════════════
# cron 线程只负责按时把 CronJob 放进 cron_queue；agent_loop 被唤起后才会消费并
# 注入 messages。教学版 s15 的新 poller 没有监听 cron_queue，所以单独的 cron
# 到点并不会像队友来信那样主动唤醒 Lead，仍需一次用户或其他异步事件触发循环。

DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"


@dataclass
class CronJob:
    id: str
    cron: str        # "0 9 * * *"
    prompt: str      # message to inject when fired
    recurring: bool  # True = recurring, False = one-shot
    durable: bool    # True = persist to disk


scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
cron_lock = threading.Lock()
_last_fired: dict[str, str] = {}  # job_id → "YYYY-MM-DD HH:MM"


def _cron_field_matches(field: str, value: int) -> bool:
    """Match a single cron field against a value."""
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(f.strip(), value)
                   for f in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """Check if a 5-field cron expression matches the given datetime.
    Standard cron semantics: DOM and DOW use OR when both are constrained."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)

    # Minute, hour, month must all match
    if not (m and h and month_ok):
        return False
    # DOM and DOW: if both constrained, either matching is enough (OR)
    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    """Validate a single cron field value is within [lo, hi]."""
    if field == "*":
        return None
    if field.startswith("*/"):
        step_str = field[2:]
        if not step_str.isdigit():
            return f"Invalid step: {field}"
        step = int(step_str)
        if step <= 0:
            return f"Step must be > 0: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err: return err
        return None
    if "-" in field:
        parts = field.split("-", 1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f"Invalid range: {field}"
        a, b = int(parts[0]), int(parts[1])
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    val = int(field)
    if val < lo or val > hi:
        return f"Value {val} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    """Validate a cron expression. Returns error message or None."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for i, (field, (lo, hi), name) in enumerate(zip(fields, bounds, names)):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def save_durable_jobs():
    """Persist durable jobs to .scheduled_tasks.json."""
    durable = [asdict(j) for j in scheduled_jobs.values() if j.durable]
    DURABLE_PATH.write_text(json.dumps(durable, indent=2))


def load_durable_jobs():
    """Load durable jobs from disk on startup."""
    if not DURABLE_PATH.exists():
        return
    try:
        jobs = json.loads(DURABLE_PATH.read_text())
        for j in jobs:
            job = CronJob(**j)
            err = validate_cron(job.cron)
            if err:
                print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
                continue
            scheduled_jobs[job.id] = job
        valid = [j for j in jobs if j["id"] in scheduled_jobs]
        if valid:
            print(f"  \033[35m[cron] loaded {len(valid)} durable job(s)\033[0m")
    except Exception:
        pass


def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    """Register a new cron job. Returns CronJob or error string."""
    err = validate_cron(cron)
    if err:
        return err
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
    )
    with cron_lock:
        scheduled_jobs[job.id] = job
    if durable:
        save_durable_jobs()
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job


def cancel_job(job_id: str) -> str:
    """Cancel a cron job."""
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs()
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f"Cancelled {job_id}"


def cron_scheduler_loop():
    """Independent daemon thread: poll every 1s, fire matching jobs.
    Individual job errors are caught to prevent one bad job from
    killing the entire scheduler thread."""
    while True:
        time.sleep(1)
        now = datetime.now()
        # marker 带日期且精确到分钟：既防止同一分钟内重复触发，也不影响次日再跑。
        minute_marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, now):
                        if _last_fired.get(job.id) != minute_marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = minute_marker
                            print(f"  \033[35m[cron fire] {job.id} → "
                                  f"{job.prompt[:40]}\033[0m")
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")


def consume_cron_queue() -> list[CronJob]:
    """Consume fired jobs from cron_queue (called by agent_loop)."""
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


# 启动时先恢复 durable job，再启动常驻调度线程。
load_durable_jobs()
threading.Thread(target=cron_scheduler_loop, daemon=True).start()
print("  \033[35m[cron] scheduler thread started\033[0m")


# Cron tool handlers

def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' → {prompt}"


def run_list_crons() -> str:
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        tag = "recurring" if j.recurring else "one-shot"
        dur = "durable" if j.durable else "session"
        lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:40]} "
                     f"[{tag}, {dur}]")
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    return cancel_job(job_id)


# ═════════════════════════════════════════════════════════════════════════════
# NEW in s15: 文件邮箱 MessageBus —— 把跨 Agent 通信变成可观察的持久状态
# ═════════════════════════════════════════════════════════════════════════════
# 这里的 “Bus” 不是一个常驻中央服务：发送方直接 append 收件人的 .jsonl 文件，
# 接收方再自行读取。选择文件而非内存 Queue，使不同线程都能通信，也便于读者在
# .mailboxes 目录直接观察消息；代价是教学实现没有文件锁，不能保证生产级并发安全。

MAILBOX_DIR = WORKDIR / ".mailboxes"
MAILBOX_DIR.mkdir(exist_ok=True)


class MessageBus:
    """File-based message bus. Each agent has a .jsonl inbox.
    Read is destructive: read_text + unlink (consumes messages).
    Teaching version: no file locking; real CC uses proper-lockfile."""

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message"):
        # from/to/type/ts 一起落盘，使普通消息和最终 result 能沿用同一传输格式。
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time()}
        # “写给谁”就追加到“谁的文件”；没有单独的路由线程参与转发。
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")
        print(f"  \033[33m[bus] {from_agent} → {to_agent}: "
              f"{content[:50]}\033[0m")

    def read_inbox(self, agent: str) -> list[dict]:
        # 消费语义是 read → unlink：返回列表的同时清空这一批消息。
        # 因为没有锁，send 恰好发生在读取与删除之间时存在丢信竞态，这是教学取舍。
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]
        inbox.unlink()  # 删除整个邮箱文件；下一封消息会重新创建它
        return msgs

    def peek(self, agent: str) -> bool:
        """Non-destructive: True if the agent has unread inbox messages.
        The Lead's inbox poller uses this to decide whether to wake a turn
        without consuming the mailbox."""
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        # poller 只按响“门铃”，真正取信必须留给主线程，避免检测动作提前消费内容。
        return inbox.exists() and inbox.stat().st_size > 0


BUS = MessageBus()

# 这是进程内的轻量活动表：用于防止重名和显示完成状态，不是持久化 Team Config。
active_teammates: dict[str, bool] = {}


# ═════════════════════════════════════════════════════════════════════════════
# NEW in s15: spawn_teammate_thread —— 每个队友拥有独立上下文的多轮 Agent
# ═════════════════════════════════════════════════════════════════════════════
# 队友不是在 Lead 的 messages 上继续推理，而是在独立 daemon 线程中创建自己的
# system、messages、tools 和循环。线程共享文件系统，但模型上下文彼此隔离；需要
# 共享的信息必须显式经过 MessageBus，这正是“队友”区别于同一上下文内分支的关键。

def spawn_teammate_thread(name: str, role: str, prompt: str) -> str:
    """Spawn a teammate agent in a background thread.
    Teaching version: max 10 rounds per teammate.
    Real CC: teammates use idle loop (wait for inbox, work, repeat)
    until shutdown_request."""
    # 同名队友还在活动时拒绝重复 spawn；教学版没有加锁，默认由 Lead 串行调用。
    if name in active_teammates:
        return f"Teammate '{name}' already exists"

    # role 决定队友身份，prompt 是它收到的第一项具体任务；两者用途不同。
    system = (f"You are '{name}', a {role}. "
              f"Use tools to complete tasks. "
              f"Send results via send_message to 'lead'.")

    def run():
        # 新建 messages 意味着队友看不到 Lead 的完整历史，只知道派发给自己的 prompt。
        messages = [{"role": "user", "content": prompt}]
        # 队友工具集刻意缩小：能操作工作区、能发消息，但不能继续 spawn 队友，
        # 也没有教学版 Lead 的 task / cron / 后台调度能力。
        sub_tools = [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object",
                              "properties": {"command": {"type": "string"}},
                              "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}},
            {"name": "write_file", "description": "Write content to a file.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["path", "content"]}},
            {"name": "send_message",
             "description": "Send a message to another agent.",
             "input_schema": {"type": "object",
                              "properties": {"to": {"type": "string"},
                                             "content": {"type": "string"}},
                              "required": ["to", "content"]}},
        ]
        sub_handlers = {
            "bash": run_bash, "read_file": run_read, "write_file": run_write,
            # sender 固定使用当前闭包中的 name，模型只能指定接收者，不能伪造来源。
            "send_message": lambda to, content: (BUS.send(name, to, content),
                                                  "Sent")[1],
        }

        # 一次迭代对应一次模型响应，而不是一个 OS 时间片。最多 10 轮只是教学版
        # 的硬上限；如果模型不再请求工具会提前退出，并不会进入可再次唤醒的 idle loop。
        for _ in range(10):
            # 队友每轮调用模型前先消费自己的邮箱，使 Lead 的后续指令进入其上下文。
            inbox = BUS.read_inbox(name)
            if inbox:
                messages.append({"role": "user",
                                 "content": f"<inbox>{json.dumps(inbox)}</inbox>"})
            try:
                # [-20:] 只限制传入的消息条目数，不等价于精确的 token 预算。
                response = client.messages.create(
                    model=MODEL, system=system, messages=messages[-20:],
                    tools=sub_tools, max_tokens=8000)
            except Exception:
                break
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break
            results = []
            # 与 Lead 一样：先执行所有 tool_use，再把对应 tool_result 作为 user 消息
            # 回填；这样下一轮模型才能看到本轮行动的真实结果。
            for block in response.content:
                if block.type == "tool_use":
                    handler = sub_handlers.get(block.name)
                    output = handler(**block.input) if handler else "Unknown"
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id,
                                    "content": str(output)})
            messages.append({"role": "user", "content": results})

        # 无论是正常结束、达到 10 轮还是 API 异常，都会尝试选取最近一段文本作为
        # summary；完全没有文本时使用 "Done."，保证 Lead 最终仍能收到一个结果事件。
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
        # 先投递结果、再移出活动表。即使 registry 已清空，邮箱文件仍能独立存活；
        # 因而 Lead 的 poller 绝不能只用 active_teammates 判断是否还有结果。
        BUS.send(name, "lead", summary, "result")
        active_teammates.pop(name, None)
        print(f"  \033[32m[teammate] {name} finished\033[0m")

    # 先登记再启动，避免线程刚运行时外部仍认为该名字可重复创建。
    active_teammates[name] = True
    # daemon=True 表示主进程退出时不会等待队友收尾；s16 才会引入正式关机握手。
    threading.Thread(target=run, daemon=True).start()
    print(f"  \033[36m[teammate] {name} spawned as {role}\033[0m")
    return f"Teammate '{name}' spawned as {role}"


# ═════════════════════════════════════════════════════════════════════════════
# NEW in s15: 团队工具 Handler —— 把模型调用连接到线程与邮箱
# ═════════════════════════════════════════════════════════════════════════════
# spawn_teammate 管生命周期，send_message 负责 Lead → 队友，check_inbox 则让
# Lead 可以主动取信。后两者都复用 MessageBus，但发送者身份由 handler 固定。

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    # Lead 无需也不能从 tool input 自报身份，避免把消息来源交给模型随意填写。
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_check_inbox() -> str:
    # 这是主动查询路径，同样会消费邮箱；结果会作为 tool_result 回到当前模型轮次。
    # 文件末尾的自动 poller 则是不经模型主动调用的被动唤醒路径。
    msgs = BUS.read_inbox("lead")
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        lines.append(f"  [{m['from']}] {m['content'][:200]}")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# FROM s02-s14 (unchanged): Lead 原有工具 schema；s15 在同一列表追加团队入口
# ═════════════════════════════════════════════════════════════════════════════
# schema 是给模型看的能力说明；名称必须与 execute_tool 中的 handler key 对齐。

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object",
                      "properties": {
                          "command": {"type": "string"},
                          "run_in_background": {"type": "boolean"}},
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
    {"name": "create_task",
     "description": "Create a new task with optional blockedBy dependencies.",
     "input_schema": {"type": "object",
                      "properties": {
                          "subject": {"type": "string"},
                          "description": {"type": "string"},
                          "blockedBy": {"type": "array",
                                        "items": {"type": "string"}}},
                      "required": ["subject"]}},
    {"name": "list_tasks",
     "description": "List all tasks with status, owner, and dependencies.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "get_task",
     "description": "Get full details of a specific task by ID.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "claim_task",
     "description": "Claim a pending task. Sets owner, changes status to in_progress.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "complete_task",
     "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
     "input_schema": {"type": "object",
                      "properties": {"task_id": {"type": "string"}},
                      "required": ["task_id"]}},
    {"name": "schedule_cron",
     "description": "Schedule a cron job. cron is 5-field: min hour dom month dow.",
     "input_schema": {"type": "object",
                      "properties": {
                          "cron": {"type": "string",
                                   "description": "5-field cron expression"},
                          "prompt": {"type": "string",
                                     "description": "Message to inject when fired"},
                          "recurring": {"type": "boolean",
                                        "description": "True=recurring, False=one-shot"},
                          "durable": {"type": "boolean",
                                      "description": "True=persist to disk"}},
                      "required": ["cron", "prompt"]}},
    {"name": "list_crons",
     "description": "List all registered cron jobs.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
    {"name": "cancel_cron",
     "description": "Cancel a cron job by ID.",
     "input_schema": {"type": "object",
                      "properties": {"job_id": {"type": "string"}},
                      "required": ["job_id"]}},
    # ─────────────────────────────────────────────────────────────────────────
    # NEW in s15: Lead 可见的三个团队协作工具
    # ─────────────────────────────────────────────────────────────────────────
    # spawn 创建独立队友，send 投递消息，check 主动消费 Lead 邮箱；自动收信机制
    # 不属于工具，而位于 __main__ 的 inbox_poller + events 主循环中。
    {"name": "spawn_teammate",
     "description": "Spawn a teammate agent in a background thread.",
     "input_schema": {"type": "object",
                      "properties": {
                          "name": {"type": "string"},
                          "role": {"type": "string"},
                          "prompt": {"type": "string"}},
                      "required": ["name", "role", "prompt"]}},
    {"name": "send_message",
     "description": "Send a message to a teammate via MessageBus.",
     "input_schema": {"type": "object",
                      "properties": {"to": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["to", "content"]}},
    {"name": "check_inbox",
     "description": "Check Lead's inbox for teammate messages.",
     "input_schema": {"type": "object", "properties": {},
                      "required": []}},
]


# ═════════════════════════════════════════════════════════════════════════════
# FROM s09-s14 (unchanged): 从运行时状态派生 Lead 的 Prompt Context
# ═════════════════════════════════════════════════════════════════════════════
# 函数结构未变，但 enabled_tools 会自然包含 s15 新增 schema；这体现了“能力从
# 注册表派生”，不需要再为团队机制复制一套 prompt 组装逻辑。

def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state."""
    memories = ""
    if MEMORY_INDEX.exists():
        content = MEMORY_INDEX.read_text().strip()
        if content:
            memories = content
    return {
        "enabled_tools": [t["name"] for t in TOOLS],
        "workspace": str(WORKDIR),
        "memories": memories,
    }


# ═════════════════════════════════════════════════════════════════════════════
# FROM s01-s14 (unchanged): Lead 的基础 Agent Loop
# ═════════════════════════════════════════════════════════════════════════════
# s15 没把团队调度硬编码进这条循环：它仍只做“调用模型 → 执行工具 → 回填结果”。
# 队友消息是在外层先变成普通 user message，再复用同一个 agent_loop；这使新增的
# 异步来源不会污染最核心的工具循环。教学版也继续省略 s11 的完整错误恢复。

def agent_loop(messages: list, context: dict):
    system = get_system_prompt(context)
    while True:
        # 每次进入模型前顺手消费已触发 cron；这里是注入点，不是 cron 的唤醒源。
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if should_run_background(block.name, block.input):
                # 先立即回告“已启动”，真实完成结果稍后通过统一 wake 通道进入新 turn。
                bg_id = start_background_task(block)
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": f"[Background task {bg_id} started] "
                                           f"Result will be available when complete."})
            else:
                output = execute_tool(block)
                print(str(output)[:300])
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})

        # 同步 tool_result 与此刻已经完成的后台通知合并成一个 user 消息，保持
        # Anthropic tool_use/tool_result 的轮次配对关系。
        user_content = list(results)
        bg_notifications = collect_background_results()
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})
        messages.append({"role": "user", "content": user_content})
        context = update_context(context, messages)
        system = get_system_prompt(context)


# ═════════════════════════════════════════════════════════════════════════════
# NEW in s15: 异步事件合流、Lead 自动唤醒与 inbox 注入
# ═════════════════════════════════════════════════════════════════════════════
# 这是本章闭环的最后一段。input_reader 和 inbox_poller 可以并行等待，但都只把
# 事件放入线程安全 Queue；真正修改 history、消费邮箱和调用 agent_loop 的始终是
# 下方唯一的主线程。因此“后台并发生产事件”和“前台串行推进对话”可以同时成立。

if __name__ == "__main__":
    print("s15: agent teams")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = update_context({}, [])

    # input() 会阻塞当前线程，所以把它移入独立 reader；否则 Lead 等用户敲键盘时，
    # 即使队友已经写完邮箱，主流程也没有机会处理。两类来源最终汇入同一个队列。
    events = queue.Queue()

    def input_reader():
        # 此线程只采集终端输入，不碰 history；quit/user 也被编码成普通事件。
        while True:
            try:
                line = input("\033[36ms15 >> \033[0m")
            except (EOFError, KeyboardInterrupt):
                events.put(("quit", None))
                return
            events.put(("user", line))

    def inbox_poller():
        # 每秒只做非破坏性探测：队友邮箱或后台结果任一就绪，就投递 wake。
        # 不能用 active_teammates 作为前置条件，因为队友是“先发最终结果、再注销”，
        # 它的 inbox 消息可能比活动表记录活得更久。
        while True:
            time.sleep(1)
            if BUS.peek("lead") or has_pending_background():
                events.put(("wake", None))

    # 两个生产者可以并发等待；消费 events 的仍只有下面一个主循环。
    threading.Thread(target=input_reader, daemon=True).start()
    threading.Thread(target=inbox_poller, daemon=True).start()

    had_teammates = False
    while True:
        # 没有用户输入也没有异步结果时在这里阻塞，不会空转消耗 CPU。
        kind, payload = events.get()
        if kind == "quit":
            break
        if kind == "user":
            if payload.strip().lower() in ("q", "exit", ""):
                break
            history.append({"role": "user", "content": payload})
        else:  # "wake": teammate inbox or background results are ready
            # wake 只是“可能有新结果”的信号；内容必须在主线程此刻正式消费。
            parts = []
            inbox = BUS.read_inbox("lead")
            if inbox:
                parts.append("[Inbox]\n" + "\n".join(
                    f"From {m['from']}: {m['content'][:200]}" for m in inbox))
            bg = collect_background_results()
            parts.extend(bg)
            if not parts:
                # 轮询期间可能堆入多个 wake；第一条已取空资源，后续空 wake 直接跳过，
                # 从而让“至少一次唤醒”表现为“结果至多注入一次”。
                continue
            # 这是 Lead “感知”异步结果的关键一步：邮箱/后台结果不直接调用模型，
            # 而是先伪装成一个正常 user turn 进入 history，再由统一 agent_loop 处理。
            history.append({"role": "user", "content": "\n".join(parts)})
            print(f"\n\033[33m[wake: {len(inbox)} inbox + {len(bg)} background "
                  f"-> new turn]\033[0m")

        # 不论来源是人类输入还是异步 wake，最终都复用同一条 Lead 推理路径。
        agent_loop(history, context)
        context = update_context(context, history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
            elif isinstance(block, dict) and block.get("type") == "text":
                print(block.get("text", ""))

        # “线程已退出”还不等于“协作已收尾”：只有活动表为空，且最后一封邮件与
        # 后台通知也已被消费，才打印一次 all teammates done，避免过早宣告完成。
        if active_teammates:
            had_teammates = True
        elif had_teammates and not BUS.peek("lead") and not has_pending_background():
            print("\033[32m[all teammates done]\033[0m")
            had_teammates = False
        print()
