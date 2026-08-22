#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""向 ZCode 桌面端派活（Codex -> ZCode 协作派发器）。

原理：向 ZCode 桌面端的 automations 调度库注入一行一次性任务，
桌面端调度器（每 20s 扫描）会认领并为它创建一个全新任务（UI 实时可见、
流式输出），prompt 执行完毕后结果可从会话库或落盘报告文件回收。

用法：
  python dispatch.py --task-file <任务文件.txt> --cwd <仓库根目录> [--title 标题]
                     [--target-task <sessionId>] [--timeout-sec 1200] [--dry-run]

约定（与 dsh-dispatch 一致）：
  - 任务文本写成 UTF-8 .txt 文件，必须自包含（目标/白名单/红线/测试/落盘报告五要素）。
  - 报告文件默认 <仓库根>\\Zcodedispatch\\zcode-report.md，派单前自动备份旧报告。
"""

import argparse
import json
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

TASKS_DB = Path.home() / ".zcode" / "v2" / "tasks-index.sqlite"
SESSION_DB = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"

DEFAULT_MODEL = "builtin:bigmodel/GLM-5.3"
DEFAULT_PROVIDER = "glm"
DEFAULT_MODE = "yolo"
DEFAULT_THOUGHT_LEVEL = "max"

POLL_INTERVAL = 2
CLAIM_WINDOW_SEC = 5   # next_run_at = now + 这么多秒；INSERT 已提交，调度器 20s tick 认领，最坏等待=delay+20s


def out(msg: str) -> None:
    print(msg, flush=True)


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 8000")
    return con


def check_schema(con: sqlite3.Connection) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(automations)")}
    required = {
        "automation_id", "title", "cron_expr", "prompt", "model", "provider",
        "workspace_key", "workspace_path", "workspace_identity", "target_task_id",
        "location_kind", "recurring", "max_runs", "end_at", "schedule_rule",
        "schedule_edited_by_user", "run_count", "enabled", "lifecycle_status",
        "next_run_at", "running", "claimed_at", "dispatch_status",
        "mode", "thought_level", "created_at", "updated_at",
    }
    missing = required - cols
    if missing:
        raise SystemExit(f"tasks-index.sqlite 结构不符（缺列: {sorted(missing)}），"
                         "ZCode 桌面端可能已升级，请暂停使用并人工核对新 schema。")


def insert_automation(args, prompt: str, now_ms: int) -> str:
    automation_id = f"automation-{uuid.uuid4()}"
    next_run_at = now_ms + args.next_run_delay_sec * 1000
    fire = datetime.now() + timedelta(seconds=args.next_run_delay_sec)
    cron_expr = f"{fire.minute} {fire.hour} {fire.day} {fire.month} *"
    schedule_rule = json.dumps({
        "unit": "minute", "interval": 1,
        "hour": fire.hour, "minute": fire.minute, "anchorAt": now_ms,
    }, ensure_ascii=False)
    ws = str(Path(args.cwd).resolve())

    row = {
        "automation_id": automation_id,
        "title": args.title,
        "cron_expr": cron_expr,
        "prompt": prompt,
        "model": args.model,
        "provider": args.provider,
        "mode": args.mode,
        "thought_level": args.thought_level,
        "workspace_key": ws,
        "workspace_path": ws,
        "workspace_identity": None,
        "target_task_id": args.target_task,
        "bot_delivery_target": None,
        "location_kind": "local",
        "recurring": 0,
        "max_runs": None,
        "end_at": None,
        "schedule_rule": schedule_rule,
        "schedule_edited_by_user": 0,
        "run_count": 0,
        "scheduled_run_count": 0,
        "enabled": 1,
        "lifecycle_status": "active",
        "next_run_at": next_run_at,
        "last_run_at": None,
        "running": 0,
        "claimed_at": None,
        "dispatch_status": "idle",
        "dispatch_attempts": 0,
        "retry_at": None,
        "last_error": None,
        "created_at": now_ms,
        "updated_at": now_ms,
    }
    cols = ",".join(row)
    marks = ",".join("?" for _ in row)
    con = connect(TASKS_DB)
    try:
        check_schema(con)
        con.execute("BEGIN IMMEDIATE")
        con.execute(f"INSERT INTO automations ({cols}) VALUES ({marks})",
                    list(row.values()))
        con.execute("COMMIT")
    finally:
        con.close()
    return automation_id


def last_assistant_text(session_id: str) -> str:
    """从 part 表取最后一条 assistant 文本。

    注意：message.data 里没有 text 字段（正文在 part 表），
    直接读 message.data.text 永远是空——原兜底因此从未生效。
    """
    con = connect(SESSION_DB)
    try:
        row = con.execute(
            "SELECT json_extract(p.data, '$.text') AS t "
            "FROM part p JOIN message m ON p.message_id = m.id "
            "WHERE p.session_id = ? "
            "AND json_extract(m.data, '$.role') = 'assistant' "
            "AND json_extract(p.data, '$.type') = 'text' "
            "ORDER BY m.sequence DESC, p.sequence DESC LIMIT 1",
            (session_id,)).fetchone()
        return str(row["t"] or "") if row else ""
    finally:
        con.close()


def lookup_run(automation_id: str):
    con = connect(TASKS_DB)
    try:
        return con.execute(
            "SELECT session_id, outcome, error, dispatch_status FROM automation_runs "
            "WHERE run_id LIKE ? ORDER BY created_at DESC LIMIT 1",
            (automation_id + ":%",)).fetchone()
    finally:
        con.close()


def wait_for_result(args, automation_id: str, report: Path,
                    deadline: float) -> tuple[str, str]:
    """回收：报告文件是唯一完成信号；run succeeded 后用会话最后回复兜底。

    兜底必须 gate 在 outcome='succeeded' 之后——ZCode 任务流式执行，
    中途会产生大量 assistant 消息，不能见到第一条就收工。
    """
    session_id = ""
    announced_claim = False
    announced_fallback = False
    out(f"[等待] 轮询结果（超时 {args.timeout_sec}s），报告: {report}")
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)

        run = lookup_run(automation_id)
        if run is not None:
            session_id = session_id or (run["session_id"] or "")
            if run["outcome"] == "failed" or (
                    run["error"] and run["dispatch_status"] != "dispatched"):
                raise SystemExit(f"ZCode 任务失败: {run['error']}")
        elif not announced_claim:
            con = connect(TASKS_DB)
            try:
                a = con.execute(
                    "SELECT dispatch_status FROM automations WHERE automation_id = ?",
                    (automation_id,)).fetchone()
            finally:
                con.close()
            if a and a["dispatch_status"] == "claimed":
                out("[状态] 已被调度器认领，等待任务创建……")
                announced_claim = True

        if report.exists():
            try:
                text = report.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if text.strip():
                return session_id, text

        if run is not None and run["outcome"] == "succeeded" and session_id:
            if not announced_fallback:
                out(f"[状态] run succeeded，session={session_id}；报告文件未出现，"
                    "改用会话最后一条回复兜底")
                announced_fallback = True
            last = last_assistant_text(session_id)
            if last.strip():
                return session_id, last
    return session_id, ""


def main() -> None:
    ap = argparse.ArgumentParser(description="Codex -> ZCode 桌面端派活")
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--target-task", default=None,
                    help="绑定已有任务 sessionId 复用（类似 dsh -ReuseSession）；"
                         "缺省=每次派活创建全新任务（推荐）")
    ap.add_argument("--next-run-delay-sec", type=int, default=CLAIM_WINDOW_SEC)
    ap.add_argument("--timeout-sec", type=int, default=1200)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--provider", default=DEFAULT_PROVIDER)
    ap.add_argument("--mode", default=DEFAULT_MODE)
    ap.add_argument("--thought-level", default=DEFAULT_THOUGHT_LEVEL)
    ap.add_argument("--report-name", default="zcode-report.md")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    task_file = Path(args.task_file)
    if not task_file.is_file():
        raise SystemExit(f"任务文件不存在: {task_file}")
    prompt = task_file.read_text(encoding="utf-8").strip()
    if not prompt:
        raise SystemExit(f"任务文件为空: {task_file}")
    if not args.title:
        args.title = task_file.stem

    if args.target_task and not args.target_task.startswith("sess_"):
        raise SystemExit("--target-task 必须是 sess_ 开头的 sessionId")

    report_dir = Path(args.cwd).resolve() / "Zcodedispatch"
    report = report_dir / args.report_name

    if args.dry_run:
        out(f"cwd        = {args.cwd}")
        out(f"model      = {args.provider}/{args.model} mode={args.mode} "
            f"thought={args.thought_level}")
        out(f"target     = {args.target_task or '(全新任务)'}")
        out(f"报告文件    = {report}")
        out(f"任务文本    = {task_file}（{len(prompt)} 字符）")
        out("流程: INSERT automations(one-shot) -> 桌面端调度器认领(<=20s+delay) "
            "-> 新建任务并 sendPrompt -> 轮询报告/会话消息")
        return

    if not TASKS_DB.is_file():
        raise SystemExit(f"未找到 ZCode 任务索引库: {TASKS_DB}（ZCode 桌面端装了吗？）")

    report_dir.mkdir(parents=True, exist_ok=True)
    if report.exists():
        prev = Path.home() / ".zcode" / "Zcodedispatch-backup" / (
            f"zcode-report-prev-{datetime.now():%Y%m%d-%H%M%S}.md")
        prev.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(report), str(prev))
        out(f"旧报告已备份到 {prev}")

    now_ms = int(time.time() * 1000)
    automation_id = insert_automation(args, prompt, now_ms)
    out(f"[派单] automation_id = {automation_id}")
    out(f"[派单] {args.next_run_delay_sec}s 后触发；保持 ZCode 桌面端处于运行状态")

    session_id, result = wait_for_result(args, automation_id, report,
                                         time.time() + args.timeout_sec)
    if not result:
        out(f"TIMEOUT: {args.timeout_sec}s 未收到结果（任务可能仍在 ZCode 界面继续，"
            f"automation_id={automation_id}）")
        sys.exit(1)
    out("===== ZCode 结果 =====")
    out(result)
    out(f"sessionId = {session_id or '(未见，读报告文件)'}")


if __name__ == "__main__":
    main()
