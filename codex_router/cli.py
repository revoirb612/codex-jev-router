from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "codex-router"
DB_PATH = APP_DIR / "jobs.sqlite3"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(path: Path | None) -> dict:
    config_path = path or DEFAULT_CONFIG
    if not config_path.exists():
        example = Path(__file__).resolve().parent.parent / "config.example.toml"
        raise SystemExit(
            f"Missing config: {config_path}\n"
            f"Copy {example} to {config_path} and edit the model names."
        )
    with config_path.open("rb") as stream:
        return tomllib.load(stream)


def connect_db() -> sqlite3.Connection:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            prompt TEXT NOT NULL,
            route TEXT NOT NULL,
            model TEXT NOT NULL,
            session_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    return db


def jev_route(prompt: str, config: dict) -> tuple[str, float]:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise SystemExit("Set TYPESAFE_API_KEY before routing a task.")
    routing = config["routing"]
    body = {
        "model": routing.get("model", "jev-latest"),
        "state": prompt,
        "questions": {
            "task_complexity": {
                "type": "choice",
                "instructions": (
                    "Choose quick for a small, bounded task that is easy to verify. "
                    "Choose deep for multi-file changes, debugging, design tradeoffs, "
                    "or tasks where mistakes are costly."
                ),
                "criteria": {
                    routing.get("quick_label", "quick"): "Small, bounded, low-risk task",
                    routing.get("deep_label", "deep"): "Complex, broad, or high-risk task",
                },
            }
        },
    }
    request = Request(
        "https://api.typesafe.ai/v1/systemone",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise SystemExit(f"Jev request failed: {exc}") from exc
    answer = result.get("answers", {}).get("task_complexity", {})
    probs = answer.get("probabilities", {})
    quick = routing.get("quick_label", "quick")
    deep = routing.get("deep_label", "deep")
    label = answer.get("choice")
    probability = float(probs.get(label, 0.0))
    threshold = float(routing.get("minimum_probability", 0.65))
    if label not in (quick, deep) or probability < threshold:
        return deep, probability
    return label, probability


def stream_codex(args: list[str], job_id: str, db: sqlite3.Connection, cwd: str | None = None) -> int:
    if args and args[0] == "resume":
        command = ["codex", "exec", "resume", "--json", *args[1:]]
    else:
        command = ["codex", "exec", "--json", *args]
    print(f"Job {job_id}\n$ {' '.join(command)}", flush=True)
    db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", ("running", now(), job_id))
    db.commit()
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=None, text=True, encoding="utf-8")
    session_id = None
    try:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\r\n")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(line, flush=True)
                continue
            if event.get("type") == "thread.started":
                session_id = event.get("thread_id")
                db.execute(
                    "UPDATE jobs SET session_id=?, updated_at=? WHERE id=?",
                    (session_id, now(), job_id),
                )
                db.commit()
                print(f"Codex session: {session_id}", flush=True)
            item = event.get("item", {})
            if item.get("type") == "agent_message" and item.get("text"):
                print(item["text"], flush=True)
        return_code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", ("interrupted", now(), job_id))
        db.commit()
        print(f"\nInterrupted. Resume with: codex-router resume {job_id}", file=sys.stderr)
        return 130
    status = "complete" if return_code == 0 else "failed"
    db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", (status, now(), job_id))
    db.commit()
    return return_code


def run_task(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise SystemExit(f"Repository path does not exist: {repo}")
    config = load_config(args.config)
    route, probability = jev_route(args.task, config)
    models = config["models"]
    quick = config["routing"].get("quick_label", "quick")
    model = models["quick"] if route == quick else models["deep"]
    job_id = uuid.uuid4().hex[:12]
    db = connect_db()
    db.execute(
        "INSERT INTO jobs(id,repo,prompt,route,model,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (job_id, str(repo), args.task, route, model, "queued", now(), now()),
    )
    db.commit()
    print(f"Jev route: {route} (probability {probability:.2f}); Codex model: {model}")
    codex = config.get("codex", {})
    return stream_codex(
        [
            "--cd", str(repo),
            "--model", model,
            "--sandbox", codex.get("sandbox", "workspace-write"),
            args.task,
        ],
        job_id,
        db,
    )


def resume_task(args: argparse.Namespace) -> int:
    db = connect_db()
    row = db.execute(
        "SELECT repo,prompt,model,session_id FROM jobs WHERE id=?", (args.job_id,)
    ).fetchone()
    if not row:
        raise SystemExit(f"Unknown job: {args.job_id}")
    repo, _, model, session_id = row
    if not session_id:
        raise SystemExit("This job has no saved Codex session ID to resume.")
    prompt = args.message or "Continue the task from its current state. Review what is done and finish the original request."
    db.execute("UPDATE jobs SET status=?, updated_at=? WHERE id=?", ("queued", now(), args.job_id))
    db.commit()
    return stream_codex(["resume", session_id, "--model", model, prompt], args.job_id, db, cwd=repo)


def list_jobs(_: argparse.Namespace) -> int:
    db = connect_db()
    rows = db.execute(
        "SELECT id,status,route,model,repo,session_id,updated_at FROM jobs ORDER BY created_at DESC LIMIT 30"
    ).fetchall()
    for row in rows:
        print("\t".join(str(value or "") for value in row))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="codex-router", description="Route tasks between Jev and Codex CLI.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Classify a task with Jev, then run it with Codex CLI")
    run.add_argument("--repo", required=True, help="Project directory Codex may work in")
    run.add_argument("--task", required=True, help="Task request")
    run.add_argument("--config", type=Path, help="TOML configuration file")
    run.set_defaults(func=run_task)
    resume = sub.add_parser("resume", help="Resume a saved Codex job")
    resume.add_argument("job_id")
    resume.add_argument("--message", help="Optional follow-up instruction")
    resume.set_defaults(func=resume_task)
    jobs = sub.add_parser("jobs", help="List recent local jobs")
    jobs.set_defaults(func=list_jobs)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()