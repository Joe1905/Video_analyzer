"""Persistent DuoPlus schedules and per-attempt history; no cloud calls on import."""
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as daytime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FALLBACK_ZONE = "America/Los_Angeles"
TERMINAL = {"complete", "failed", "cancelled", "skipped", "interrupted"}
_WORKERS = {}
_WORKER_LOCK = threading.Lock()


def window(plan, now):
    """Return today's or the next available daily window, without catch-up runs."""
    zone = ZoneInfo(plan.get("timezone", FALLBACK_ZONE))
    offset = plan.get("clock_offset", 0)
    current = datetime.fromtimestamp(now + offset, zone)
    first, last = date.fromisoformat(plan["start_date"]), date.fromisoformat(plan["end_date"])
    day = max(first, current.date())
    begin, end = daytime.fromisoformat(plan["start_time"]), daytime.fromisoformat(plan["end_time"])
    while day <= last:
        left = datetime.combine(day, begin, zone).timestamp() - offset
        right = datetime.combine(day, end, zone).timestamp() - offset
        if left < right and now < right:
            return max(now, left), right
        day += timedelta(days=1)
    return None


def validate_plan(body):
    import duoplus_workflow as workflow
    plan = {key: body.get(key) for key in ("start_date", "end_date", "start_time", "end_time")}
    try:
        first, last = date.fromisoformat(plan["start_date"]), date.fromisoformat(plan["end_date"])
        begin, end = daytime.fromisoformat(plan["start_time"]), daytime.fromisoformat(plan["end_time"])
    except (TypeError, ValueError):
        raise ValueError("请填写有效的起止日期与每日开始/结束时间。")
    if first > last or begin >= end or begin.tzinfo or end.tzinfo:
        raise ValueError("结束日期不能早于开始日期；每日结束时间须晚于开始时间。")
    if type(body.get("repeat")) is not bool:
        raise ValueError("是否循环必须为布尔值。")
    plan.update(repeat=body["repeat"], interval_seconds=workflow.integer(body.get("interval_seconds"), 60, 2592000))
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("缺少工作流执行参数。")
    source, _, options = workflow.prepare(payload)
    # Only persist explicitly supported execution options, never credentials.
    plan["payload"] = {key: options[key] for key in
                       ("mode", "topics", "image_id", "budget_seconds", "ai_max_steps",
                        "auto_power_off", "auto_power_on", "card_key", "name")}
    plan["payload"]["topics"] = "\n".join(options["topics"])
    plan["payload"]["workflow"] = source
    if plan["repeat"] and options["auto_power_off"] and not options["auto_power_on"]:
        raise ValueError("循环且结束后关机时，请启用到点自动开机，或取消结束后关机。")
    plan["card_key"], plan["name"] = options["card_key"], options["name"]
    return plan


class Store:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "duoplus_workflows.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, card_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    plan_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'paused', next_run_at REAL,
                    active_run_id TEXT, last_run_id TEXT);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, card_key TEXT NOT NULL, schedule_id TEXT,
                    status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
                    result_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS runs_card_time ON runs(card_key, started_at DESC);
                CREATE TABLE IF NOT EXISTS phone_clocks (
                    image_id TEXT PRIMARY KEY, timezone TEXT NOT NULL,
                    clock_offset REAL NOT NULL, observed_at REAL NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            raise ValueError("定时计划不存在。")
        value = dict(row)
        value.update(json.loads(value.pop("plan_json")))
        value["enabled"] = bool(value["enabled"])
        return value

    def schedules(self):
        with self.connection() as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM schedules ORDER BY rowid")]

    def schedule(self, schedule_id):
        with self.connection() as db:
            return self.decode(db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone())

    def save(self, body, now=None):
        now = time.time() if now is None else now
        plan = validate_plan(body)
        plan.update(self.clock_info(plan["payload"]["image_id"]))
        if window(plan, now) is None:
            raise ValueError("计划结束时间已经过去。")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM schedules WHERE card_key=?", (plan["card_key"],)).fetchone()
            if old and old["active_run_id"]:
                raise ValueError("当前定时任务尚未结束，不能修改计划。")
            schedule_id = old["id"] if old else uuid.uuid4().hex
            db.execute("""INSERT INTO schedules(id,card_key,name,plan_json) VALUES(?,?,?,?)
                ON CONFLICT(card_key) DO UPDATE SET name=excluded.name,plan_json=excluded.plan_json,
                enabled=0,status='paused',next_run_at=NULL""",
                (schedule_id, plan["card_key"], plan["name"], json.dumps(plan, ensure_ascii=False)))
        return self.schedule(schedule_id)

    def enable(self, schedule_id, enabled, now=None, clock_info=None):
        if type(enabled) is not bool:
            raise ValueError("启用状态必须为布尔值。")
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self.decode(db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone())
            if clock_info and not plan["active_run_id"]:
                plan.update(clock_info)
            span = window(plan, now)
            if enabled and span is None:
                raise ValueError("计划已过期，请修改起止日期。")
            stored = {key: value for key, value in plan.items() if key not in {
                "id", "enabled", "status", "next_run_at", "active_run_id", "last_run_id"}}
            db.execute("""UPDATE schedules SET enabled=?,status=CASE WHEN active_run_id IS NOT NULL
                THEN 'running' ELSE ? END,next_run_at=?,plan_json=? WHERE id=?""",
                (int(enabled), "waiting" if enabled else "paused", span[0] if enabled else None,
                 json.dumps(stored, ensure_ascii=False), schedule_id))
        return self.schedule(schedule_id)

    def clock_info(self, image_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM phone_clocks WHERE image_id=?", (image_id,)).fetchone()
        if row:
            return {"timezone": row["timezone"], "clock_offset": row["clock_offset"],
                    "clock_observed_at": row["observed_at"], "clock_source": "device"}
        return {"timezone": FALLBACK_ZONE, "clock_offset": 0, "clock_observed_at": None,
                "clock_source": "fallback"}

    def put_run(self, job):
        result = {key: value for key, value in job.items() if key not in {"started_monotonic", "screen"}}
        result.setdefault("started_at", time.time())
        result.setdefault("logs", [])
        result.setdefault("error", "")
        with self.connection() as db:
            db.execute("""INSERT INTO runs VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                finished_at=excluded.finished_at,result_json=excluded.result_json""",
                (result["id"], result.get("card_key", "legacy"), result.get("schedule_id"),
                 result["status"], result["started_at"], result.get("finished_at"),
                 json.dumps(result, ensure_ascii=False)))

    def run(self, run_id):
        with self.connection() as db:
            row = db.execute("SELECT result_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("运行记录不存在。")
        return json.loads(row[0])

    def history(self, card_key):
        with self.connection() as db:
            rows = db.execute("SELECT result_json FROM runs WHERE card_key=? ORDER BY started_at DESC LIMIT 30",
                              (card_key,)).fetchall()
        return [{key: value for key, value in json.loads(row[0]).items()
                 if key not in {"logs", "screen"}} for row in rows]

    def claim(self, plan, now, end):
        run_id = uuid.uuid4().hex
        job = {"id": run_id, "card_key": plan["card_key"], "name": plan["name"],
               "schedule_id": plan["id"], "status": "queued", "started_at": now,
               "timezone": plan["timezone"], "clock_offset": plan.get("clock_offset", 0),
               "window_end": end, "logs": [], "error": ""}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            claimed = db.execute("""UPDATE schedules SET active_run_id=?,status='running'
                WHERE id=? AND enabled=1 AND active_run_id IS NULL AND next_run_at<=?""",
                (run_id, plan["id"], now)).rowcount
            if not claimed:
                return None
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                       (run_id, plan["card_key"], plan["id"], "queued", now, None, json.dumps(job, ensure_ascii=False)))
        return job

    def finish(self, plan, job, now):
        # A skipped lock conflict does not consume a non-looping task.
        repeat = plan["repeat"] or job["status"] == "skipped"
        finished = job.get("finished_at") or now
        span = window(plan, max(now, finished + plan["interval_seconds"])) if repeat else None
        with self.connection() as db:
            db.execute("""UPDATE schedules SET active_run_id=NULL,last_run_id=?,
                next_run_at=?,status=CASE WHEN enabled=0 THEN 'paused' ELSE ? END,
                enabled=CASE WHEN ? THEN enabled ELSE 0 END WHERE id=? AND active_run_id=?""",
                (job["id"], span[0] if span else None, "waiting" if span else
                 ("expired" if repeat else "complete"), int(bool(span)), plan["id"], job["id"]))

    def recover(self, now=None):
        now = time.time() if now is None else now
        with self.connection() as db:
            rows = db.execute("SELECT result_json FROM runs WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            job = json.loads(row[0])
            job.update(status="interrupted", finished_at=now, error="服务重启，运行中断；没有自动重试手机动作。")
            job.setdefault("logs", []).append({"kind": "error", "message": job["error"]})
            self.put_run(job)
        for plan in self.schedules():
            if plan["active_run_id"]:
                self.finish(plan, self.run(plan["active_run_id"]), now)
            elif plan["enabled"]:
                span = window(plan, max(now, plan["next_run_at"] or now))
                with self.connection() as db:
                    db.execute("UPDATE schedules SET next_run_at=?,status=?,enabled=? WHERE id=?",
                               (span[0] if span else None, "waiting" if span else "expired",
                                int(bool(span)), plan["id"]))


class Scheduler:
    def __init__(self, data_dir, clock=time.time):
        self.data_dir, self.store, self.clock = Path(data_dir), Store(data_dir), clock

    def tick(self):
        import duoplus
        import duoplus_workflow as workflow
        now = self.clock()
        for plan in self.store.schedules():
            if plan["active_run_id"]:
                job = self.store.run(plan["active_run_id"])
                if job["status"] in TERMINAL:
                    self.store.finish(plan, job, now)
                elif now >= job["window_end"]:
                    with workflow.STATE_LOCK:
                        live = workflow.JOBS.get(job["id"])
                        if live:
                            live["cancel_reason"] = "已到每日结束时间，停止后续动作。"
                            workflow.EVENTS[job["id"]].set()
                continue
            if not plan["enabled"] or (plan["next_run_at"] or 0) > now:
                continue
            span = window(plan, now)
            if span is None or span[0] > now or span[1] - now < 30:
                span = window(plan, span[1] if span and span[1] - now < 30 else now)
                with self.store.connection() as db:
                    db.execute("UPDATE schedules SET next_run_at=?,status=?,enabled=? WHERE id=? AND active_run_id IS NULL",
                               (span[0] if span else None, "waiting" if span else "expired",
                                int(bool(span)), plan["id"]))
                continue
            job = self.store.claim(plan, now, span[1])
            if job is None:
                continue
            config = {}
            try:
                if duoplus.LOCK.locked():
                    raise workflow.PhoneBusy("本站手机正在执行其他任务，跳过本次，等待下个间隔。")
                payload = dict(plan["payload"])
                payload["budget_seconds"] = min(payload["budget_seconds"], int(span[1] - now))
                config = duoplus.load_config(self.data_dir)
                workflow.start(config, payload, self.data_dir,
                               schedule_job=job)
            except Exception as exc:
                message = "本站手机正在执行其他任务。" if isinstance(exc, workflow.PhoneBusy) else str(exc)[:400]
                for secret in (config.get("api_key"), os.getenv("DEEPSEEK_API_KEY")):
                    if secret:
                        message = message.replace(secret, "[已隐藏]")
                job.update(status="skipped" if isinstance(exc, workflow.PhoneBusy) else "failed",
                           error=message, finished_at=self.clock(), logs=[{"kind": "error", "message": message}])
                self.store.put_run(job)
                self.store.finish(plan, job, self.clock())


def start_scheduler(data_dir):
    """Start exactly one scheduler per store in this Web process."""
    key = str(Path(data_dir).resolve())
    with _WORKER_LOCK:
        if key in _WORKERS:
            return _WORKERS[key]
        scheduler = Scheduler(data_dir)
        scheduler.store.recover()
        def loop():
            while True:
                try:
                    scheduler.tick()
                except Exception as exc:
                    print("DuoPlus scheduler error: " + type(exc).__name__, flush=True)
                time.sleep(2)
        thread = threading.Thread(target=loop, daemon=True, name="duoplus-scheduler")
        scheduler.thread = thread
        _WORKERS[key] = scheduler
        thread.start()
        return scheduler


def scheduler_running(data_dir):
    with _WORKER_LOCK:
        scheduler = _WORKERS.get(str(Path(data_dir).resolve()))
        return bool(scheduler and scheduler.thread.is_alive())


def read_device_clock(data_dir, image_id):
    """Read only on explicit enable/refresh; never power on to determine time."""
    import duoplus
    store = Store(data_dir)
    fallback = {"timezone": FALLBACK_ZONE, "clock_offset": 0, "clock_observed_at": None,
                "clock_source": "fallback"}
    if not duoplus.LOCK.acquire(blocking=False):
        return fallback
    try:
        config = duoplus.load_config(data_dir)
        deadline = time.monotonic() + 20
        phone = next((p for p in duoplus.list_phones(config, image_id, deadline)
                      if p.get("id") == image_id), None)
        if not phone or str(phone.get("status")) != "1":
            return fallback
        response = duoplus.request(config, "/api/v1/cloudPhone/command",
                                   {"image_id": image_id, "command": "getprop persist.sys.timezone; date +%s"},
                                   deadline=deadline)
        data = response.get("data", {})
        lines = data.get("content", "").strip().splitlines()
        if data.get("success") is not True or len(lines) != 2:
            raise ValueError("云手机未返回有效时区与时间。")
        zone = lines[0].strip()
        ZoneInfo(zone)
        observed = time.time()
        offset = int(lines[1].strip()) - observed
        with store.connection() as db:
            db.execute("INSERT OR REPLACE INTO phone_clocks VALUES(?,?,?,?)",
                       (image_id, zone, offset, observed))
        return store.clock_info(image_id)
    except Exception:
        return fallback
    finally:
        duoplus.LOCK.release()
