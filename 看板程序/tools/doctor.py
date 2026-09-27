"""Offline installation checks for the public job tracker package."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from project_paths import APP_DIR, BACKUP_DIR, DATA_DIR, EXPORT_DIR, TRACKER_DIR


REQUIRED_MODULES = ("fastapi", "uvicorn", "requests", "dotenv")
DATA_SUBDIRS = ("公司投递", "同步记录", "待更新", "邮件导出")
FEISHU_KEYS = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_TOKEN", "MAIN_TABLE_ID")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def is_placeholder(value: str) -> bool:
    lowered = value.lower().strip()
    return not lowered or any(token in lowered for token in ("xxx", "example.com", "your-account"))


def git_ignored(path: Path) -> bool | None:
    if not (TRACKER_DIR / ".git").exists() or shutil.which("git") is None:
        return None
    completed = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", str(path)],
        cwd=TRACKER_DIR,
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


def dashboard_running(url: str) -> bool:
    try:
        with urlopen(f"{url.rstrip('/')}/openapi.json", timeout=2) as response:
            payload = json.load(response)
        return payload.get("info", {}).get("title") == "嵌入式校招看板"
    except (OSError, URLError, ValueError, AttributeError):
        return False


def collect(url: str) -> list[tuple[str, str, str]]:
    checks: list[tuple[str, str, str]] = []

    def add(state: str, name: str, detail: str) -> None:
        checks.append((state, name, detail))

    add("ok", "python", sys.version.split()[0])
    for name in REQUIRED_MODULES:
        state = "ok" if importlib.util.find_spec(name) else "fail"
        add(state, f"dependency:{name}", "available" if state == "ok" else "missing")

    for relative in DATA_SUBDIRS:
        target = DATA_DIR / relative
        add("ok" if target.is_dir() else "fail", f"data:{relative}", str(target))
    add("ok" if BACKUP_DIR.is_dir() else "fail", "backup", str(BACKUP_DIR))
    add("ok" if EXPORT_DIR.is_dir() else "fail", "exports", str(EXPORT_DIR))

    env_path = APP_DIR / ".env"
    values = read_env(env_path)
    add("ok" if env_path.is_file() else "warn", "env", "present" if values else "not created")
    configured = [key for key in FEISHU_KEYS if not is_placeholder(values.get(key, ""))]
    if len(configured) == len(FEISHU_KEYS):
        add("ok", "feishu", "all required identifiers configured")
    elif configured:
        add("warn", "feishu", f"partially configured: {len(configured)}/{len(FEISHU_KEYS)}")
    else:
        add("skip", "feishu", "not configured; offline dashboard can still run")

    for path, label in (
        (env_path, ".env"),
        (DATA_DIR / "__doctor_private__.tmp", "业务数据"),
        (BACKUP_DIR.parent / "__doctor_private__.tmp", "历史备份"),
    ):
        ignored = git_ignored(path)
        if ignored is None:
            add("skip", f"git-ignore:{label}", "Git metadata unavailable (for example, a ZIP install)")
        else:
            add("ok" if ignored else "fail", f"git-ignore:{label}", str(path))

    running = dashboard_running(url)
    add("ok" if running else "warn", "dashboard", f"{url} is {'running' if running else 'not running'}")
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8765")
    parser.add_argument("--require-running", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    checks = collect(args.url)
    if args.require_running:
        checks = [
            ("fail", name, detail) if name == "dashboard" and state == "warn" else (state, name, detail)
            for state, name, detail in checks
        ]
    if args.json:
        print(json.dumps([{"state": a, "name": b, "detail": c} for a, b, c in checks], ensure_ascii=False, indent=2))
    else:
        labels = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
        for state, name, detail in checks:
            print(f"[{labels[state]}] {name}: {detail}")
        failures = sum(state == "fail" for state, _, _ in checks)
        warnings = sum(state == "warn" for state, _, _ in checks)
        print(f"Result: {failures} failure(s), {warnings} warning(s).")
    return 1 if any(state == "fail" for state, _, _ in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
