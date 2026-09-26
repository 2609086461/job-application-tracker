"""Shared paths for the tracker; persistent data lives outside the source tree."""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath

APP_DIR = Path(__file__).resolve().parent
TRACKER_DIR = APP_DIR.parent
DATA_DIR = Path(os.environ.get("JOB_TRACKER_DATA_DIR", TRACKER_DIR / "业务数据")).resolve()
BACKUP_DIR = Path(os.environ.get("JOB_TRACKER_BACKUP_DIR", TRACKER_DIR / "历史备份" / "飞书数据快照")).resolve()
EXPORT_DIR = DATA_DIR / "邮件导出"
LOG_DIR = APP_DIR / "运行日志"


def resolve_data_path(value: str, root: Path | str | None = None) -> Path:
    """Read existing Windows archive paths on either Windows or Linux."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing relative data path")
    relative = PurePosixPath(value.strip().replace("\\", "/"))
    if relative.is_absolute() or PureWindowsPath(value).drive or ".." in relative.parts:
        raise ValueError("Data path must stay relative to its root")
    base = Path(root if root is not None else DATA_DIR).resolve()
    candidate = base.joinpath(*relative.parts).resolve()
    candidate.relative_to(base)
    return candidate
