#!/usr/bin/env python3
# s14 目标：让 Agent 不再只能等待用户输入，而是能按 cron 时间表自动产生工作。
#
# 本章的主链路：
#   schedule_cron 注册任务 → scheduler 每秒判断是否到点 → cron_queue 暂存触发结果
#   → queue processor 等待 Agent 空闲 → agent_loop 把任务作为 [Scheduled] 消息执行。
#
# 阅读时应重点区分两件事：scheduler 只负责“到点入队”，并不直接调用模型；
# queue processor 只负责“空闲时交付”，并不判断时间。两者通过队列和两把锁解耦。
# Task System、提示词组装、基础工具和后台任务均沿用前章，只保留必要的衔接说明。
"""
s14: Cron Scheduler — independent daemon thread + queue processor.

Run:  python s14_cron_scheduler/code.py
Need: pip install anthropic python-dotenv + .env with ANTHROPIC_API_KEY

Changes from s13:
  - CronJob dataclass (id, cron, prompt, recurring, durable)
  - cron_matches: 5-field cron expression matching with DOM/DOW OR semantics
  - schedule_job / cancel_job: register/remove cron jobs (with validation)
  - cron_scheduler_loop: independent daemon thread, polls every 1s
  - cron_queue: thread-safe queue, scheduler writes, queue processor delivers
  - queue_processor_loop: auto-runs agent_loop when cron_queue has work
  - Durable storage: .scheduled_tasks.json (survives restart)
  - 3 new tools: schedule_cron, list_crons, cancel_cron

Four layers:
  1. Scheduler: daemon thread checks time → fires matching jobs
  2. Queue: cron_queue decouples scheduler from agent loop
  3. Queue processor: wakes the agent when queued work exists and it is idle
  4. Consumer: agent_loop consumes queued jobs and injects them into messages
"""

import os, subprocess, json, time, random, threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict

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

# ═══════════════════════════════════════════════════════════
# FROM s12-s13 (unchanged): 简化 Task System
# ═══════════════════════════════════════════════════════════
# 这一部分仍负责普通任务的依赖、认领和完成状态；它和下文的 CronJob 是两套概念：
# Task 描述“要完成的工作及其生命周期”，CronJob 描述“何时向 Agent 投递一条工作消息”。

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
    # 时间戳配合随机后缀生成教学版 ID，任务本身仍以独立 JSON 文件持久化。
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
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    # 先检查状态和前置依赖，再按生命周期把 pending 推进为 in_progress。
    # 教学版没有跨进程文件锁；本章的并发重点是后面的 Cron 调度链路。
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
    unblocked = [t.subject for t in list_tasks()
                 if t.status == "pending" and t.blockedBy and can_start(t.id)]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


# ═══════════════════════════════════════════════════════════
# FROM s10-s13 (unchanged): 系统提示词组装与缓存
# ═══════════════════════════════════════════════════════════
# s14 只在工具清单中加入三个 Cron 工具；提示词的分段组装和按 context 缓存机制未变。

PROMPT_SECTIONS = {
    "identity": "You are a coding agent. Act, don't explain.",
    "tools": "Available tools: bash, read_file, write_file, "
             "create_task, list_tasks, get_task, claim_task, complete_task, "
             "schedule_cron, list_crons, cancel_cron.",
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
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    return _last_prompt


# ═══════════════════════════════════════════════════════════
# FROM s02-s13 (unchanged): 基础文件工具与 Task 工具适配层
# ═══════════════════════════════════════════════════════════

def safe_path(p: str) -> Path:
    # 所有模型给出的相对路径都被限制在 WORKDIR 内，避免借助 ../ 越界访问。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, run_in_background: bool = False) -> str:
    # run_in_background 只用于工具 schema；是否转入后台由 agent_loop 分发层决定。
    # 真正进入这里时，命令就是当前线程中的同步执行。
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


# Task tools

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


# ═══════════════════════════════════════════════════════════
# FROM s13 (unchanged): 慢工具的后台执行与结果通知
# ═══════════════════════════════════════════════════════════
# background_lock 保护后台任务状态；它与后文保护 Cron 数据的 cron_lock 各管一套共享状态。

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
    handler = {
        "bash": run_bash, "read_file": run_read, "write_file": run_write,
        "create_task": run_create_task, "list_tasks": run_list_tasks,
        "get_task": run_get_task, "claim_task": run_claim_task,
        "complete_task": run_complete_task,
        "schedule_cron": run_schedule_cron, "list_crons": run_list_crons,
        "cancel_cron": run_cancel_cron,
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
        # worker 只执行工具并登记结果，不直接续跑 agent_loop；结果会在后续轮次被收集。
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


# ═══════════════════════════════════════════════════════════
# NEW in s14: Cron 调度器核心——注册、匹配、持久化与到点入队
# ═══════════════════════════════════════════════════════════

# 文件位于启动命令所在的 WORKDIR，而不是固定写进 s14_cron_scheduler 源码目录。
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"


@dataclass
class CronJob:
    # CronJob 只保存调度定义，不保存 Task System 的 pending/in_progress/completed 状态。
    id: str
    cron: str        # "0 9 * * *"
    prompt: str      # message to inject when fired
    recurring: bool  # True = recurring, False = one-shot
    durable: bool    # True = persist to disk


# scheduled_jobs 是“仍有效的调度定义”；cron_queue 是“已经到点、等待交付的工作”。
# 同一把 cron_lock 同时保护二者，保证 scheduler、agent_loop 与工具调用不会并发改坏容器。
scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
cron_lock = threading.Lock()

# agent_lock 不保护 Cron 数据，而是保证用户输入与 queue processor 不会同时启动两轮 Agent。
agent_lock = threading.Lock()

# scheduler 每秒都会命中同一分钟，故需记录每个 job 最近触发到哪一分钟。
# 标记包含日期，既能防止一分钟内重复入队，也不会误伤第二天同一时刻的任务。
_last_fired: dict[str, str] = {}  # job_id → "YYYY-MM-DD HH:MM"


def _cron_field_matches(field: str, value: int) -> bool:
    """Match a single cron field against a value."""
    # 教学版支持通配、步长、逗号列表、闭区间和单值；合法性由注册阶段预先保证。
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
    # Python: 周一为 0；cron: 周日为 0。加一再模 7 完成两套编号的转换。
    dow_val = (dt.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)

    # 分、时、月是 AND 关系，任一不满足即可提前失败。
    if not (m and h and month_ok):
        return False
    # 日（DOM）和星期（DOW）是标准 cron 的特殊点：
    # 其中一个为 * 时只看另一个；两者都被约束时采用 OR，而不是 AND。
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
    # matcher 为保持简单会直接 int(...)；因此所有格式和范围错误必须在注册前拦住。
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
    # 五个边界依次对应：分钟、小时、月中日期、月份、星期。
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for i, (field, (lo, hi), name) in enumerate(zip(fields, bounds, names)):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def save_durable_jobs():
    """Persist durable jobs to .scheduled_tasks.json."""
    # session-only 任务刻意不落盘；重启后只会恢复 durable=True 的定义。
    durable = [asdict(j) for j in scheduled_jobs.values() if j.durable]
    DURABLE_PATH.write_text(json.dumps(durable, indent=2))


def load_durable_jobs():
    """Load durable jobs from disk on startup."""
    # 这里只恢复调度定义，不回放进程关闭期间错过的触发时刻。
    if not DURABLE_PATH.exists():
        return
    try:
        jobs = json.loads(DURABLE_PATH.read_text())
        for j in jobs:
            job = CronJob(**j)
            # 即使磁盘文件被手工改坏，也只跳过坏 job，不让它进入 scheduler 后反复报错。
            err = validate_cron(job.cron)
            if err:
                print(f"  \033[31m[cron] skipping invalid job {job.id}: {err}\033[0m")
                continue
            scheduled_jobs[job.id] = job
        valid = [j for j in jobs if j["id"] in scheduled_jobs]
        if valid:
            print(f"  \033[35m[cron] loaded {len(valid)} durable job(s)\033[0m")
    # 教学版选择“加载失败也继续启动”；这意味着损坏的文件不会拖垮 Agent，
    # 但其中的 durable 任务也不会被恢复。
    except Exception:
        pass


def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True) -> CronJob | str:
    """Register a new cron job. Returns CronJob or error string."""
    # 先校验再写共享状态，避免坏表达式注册成功后在后台线程中持续抛异常。
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
    # durable 的含义仅是“任务定义可跨重启恢复”；Agent 进程关闭时不会在后台执行。
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
    # 删除 durable job 后立即重写文件，避免它在下次启动时“复活”。
    if job.durable:
        save_durable_jobs()
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f"Cancelled {job_id}"


def cron_scheduler_loop():
    """Independent daemon thread: poll every 1s, fire matching jobs.
    Individual job errors are caught to prevent one bad job from
    killing the entire scheduler thread."""
    # 这是生产者线程：只判断时间并入队，绝不在这里调用 LLM 或执行具体工作。
    while True:
        time.sleep(1)
        # datetime.now() 表示所有表达式均按运行 Agent 的本地时区解释。
        now = datetime.now()
        minute_marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            # 使用快照遍历，因为 one-shot job 命中后会从 scheduled_jobs 中删除。
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, now):
                        # 轮询频率是 1 秒，而 cron 最细粒度是 1 分钟；同一分钟只允许入队一次。
                        if _last_fired.get(job.id) != minute_marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = minute_marker
                            print(f"  \033[35m[cron fire] {job.id} → "
                                  f"{job.prompt[:40]}\033[0m")
                        # one-shot 在“成功入队”后便视为已触发，而不是等 Agent 执行成功后再删除。
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                # 单个 job 的异常被隔离，避免整个 daemon 线程退出后所有定时任务都失效。
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")


def consume_cron_queue() -> list[CronJob]:
    """Consume fired jobs from cron_queue (called by agent_loop)."""
    # 在锁内做“复制 + 清空”，把当前批次一次性交给消费者；新到任务会留给下一批。
    with cron_lock:
        fired = list(cron_queue)
        cron_queue.clear()
    return fired


def has_cron_queue() -> bool:
    """Return whether fired cron jobs are waiting to be delivered."""
    with cron_lock:
        return bool(cron_queue)


# 模块加载时先恢复调度定义，再启动 producer；这样不会出现线程先跑、任务后加载的窗口。
# daemon=True 表示主进程退出时无需等待该无限循环线程。
load_durable_jobs()
threading.Thread(target=cron_scheduler_loop, daemon=True).start()
print("  \033[35m[cron] scheduler thread started\033[0m")


# ═══════════════════════════════════════════════════════════
# NEW in s14: 暴露给模型的 Cron 工具适配层
# ═══════════════════════════════════════════════════════════
# 下列函数把内部对象/错误转换成模型容易继续处理的文本；核心状态仍由上面的函数维护。

def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    return f"Scheduled {result.id}: '{cron}' → {prompt}"


def run_list_crons() -> str:
    # 先在锁内复制快照，格式化字符串时无需长时间占用 cron_lock。
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


# ═══════════════════════════════════════════════════════════
# FROM s02-s13 + NEW in s14: 工具 schema（新增三个 Cron 工具）
# ═══════════════════════════════════════════════════════════
# schema 告诉模型有哪些参数；cron 的字段数与取值范围仍由 validate_cron 在运行时校验。

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
    # s14 新增：创建、查看、取消调度。recurring/durable 未传时使用处理函数的 True 默认值。
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
]


# ═══════════════════════════════════════════════════════════
# FROM s10-s13 (unchanged): 从真实状态刷新上下文
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
# NEW in s14: agent_loop 作为 Cron 队列的消费者
# ═══════════════════════════════════════════════════════════
# 教学版保留简化 agent loop，省略 s11 的完整错误恢复。scheduler 生产工作，
# queue processor 负责唤醒，而这里负责把已触发任务变成模型真正能看到的消息。

def agent_loop(messages: list, context: dict) -> dict:
    system = get_system_prompt(context)
    while True:
        # 第四层 Consumer：一次取走当前批次，并以普通 user role 注入同一会话历史。
        # [Scheduled] 只是提示模型消息来源，不是 Anthropic API 的新角色类型。
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[inject cron] {job.prompt[:50]}\033[0m")

        # 若 Agent 正在一轮多步工具调用中，期间新到的 cron 也可在下一次 while 迭代被消费；
        # 若本轮已经结束，则由 queue processor 稍后单独拉起一轮。
        try:
            response = client.messages.create(
                model=MODEL, system=system, messages=messages,
                tools=TOOLS, max_tokens=8000)
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text",
                 "text": f"[Error] {type(e).__name__}: {e}"}]})
            return context

        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return context

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"\033[36m> {block.name}\033[0m")

            if should_run_background(block.name, block.input):
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

        # 沿用 s13：当前工具结果与已经完成的后台通知合并成下一条 user 消息。
        user_content = list(results)
        bg_notifications = collect_background_results()
        if bg_notifications:
            for notif in bg_notifications:
                user_content.append({"type": "text", "text": notif})
        messages.append({"role": "user", "content": user_content})
        context = update_context(context, messages)
        system = get_system_prompt(context)


session_history: list = []
session_context = update_context({}, [])


def print_latest_assistant_text(messages: list):
    """Print text blocks from the latest assistant message."""
    if not messages:
        return
    msg = messages[-1]
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return
    content = msg.get("content", "")
    if isinstance(content, str):
        print(content)
        return
    for block in content:
        if getattr(block, "type", None) == "text":
            print(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            print(block.get("text", ""))


def run_agent_turn_locked(user_query: str | None = None):
    """Run one agent turn. Caller must hold agent_lock."""
    global session_context
    # queue processor 调用时 user_query=None：不伪造交互输入，真正的新消息由
    # agent_loop 开头从 cron_queue 消费；用户主动输入时才在这里追加消息。
    if user_query is not None:
        session_history.append({"role": "user", "content": user_query})
    session_context = agent_loop(session_history, session_context)
    session_context = update_context(session_context, session_history)
    print_latest_assistant_text(session_history)
    print()


# ═══════════════════════════════════════════════════════════
# NEW in s14: Queue Processor——Agent 空闲时自动交付已触发任务
# ═══════════════════════════════════════════════════════════
# 教学版只用 agent_lock 表示忙闲；真实系统还会考虑 UI 阻塞、队列优先级和消息模式。
def queue_processor_loop():
    """Auto-deliver fired cron jobs when the agent is idle."""
    global session_context
    while True:
        # 0.2 秒轮询关注的是“队列里是否已有工作”，与 scheduler 的 1 秒时间轮询职责不同。
        time.sleep(0.2)
        if not has_cron_queue():
            continue
        # 非阻塞抢锁：用户或另一轮 Agent 正忙时不等待占住线程，下一次循环再尝试。
        if not agent_lock.acquire(blocking=False):
            continue
        try:
            # 拿到执行权后再次确认，避免根据已经过期的队列状态启动空轮次。
            if not has_cron_queue():
                continue
            print("\n  \033[35m[queue processor] delivering scheduled work\033[0m")
            run_agent_turn_locked()
        finally:
            # 无论模型调用是否出错，都必须释放执行锁，否则后续用户输入和定时任务都会饿死。
            agent_lock.release()


if __name__ == "__main__":
    print("s14: cron scheduler")
    print("Enter a question, press Enter to send. Type q to quit.\n")
    # scheduler 在模块初始化阶段已经启动；交付线程只在脚本直接运行时启动。
    threading.Thread(target=queue_processor_loop, daemon=True).start()
    print("  \033[35m[queue processor] started\033[0m")
    while True:
        try:
            query = input("\033[36ms14 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        # 主输入线程与 queue processor 共用 agent_lock，保证同一份会话历史只有一个写入者。
        with agent_lock:
            run_agent_turn_locked(query)
