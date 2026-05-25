from __future__ import annotations

from collections import deque
from pathlib import Path


def expand_path(value: str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser().resolve()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_last_lines(path: Path, lines: int) -> str:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if lines <= 0:
        return ""
    tail = deque(maxlen=lines)
    with path.open() as handle:
        for line in handle:
            tail.append(line.rstrip("\n"))
    return "\n".join(tail)


def raw_loop_dir(state_root: Path, loop_id: str) -> Path:
    return state_root / loop_id


def loop_dir(state_root: Path, loop_id: str) -> Path:
    return ensure_dir(raw_loop_dir(state_root, loop_id))


def state_file(state_root: Path, loop_id: str) -> Path:
    return loop_dir(state_root, loop_id) / "state.json"


def events_file(state_root: Path, loop_id: str) -> Path:
    return loop_dir(state_root, loop_id) / "events.jsonl"


def log_dir(state_root: Path, loop_id: str) -> Path:
    return ensure_dir(loop_dir(state_root, loop_id) / "logs")


def lock_file(state_root: Path, loop_id: str) -> Path:
    return loop_dir(state_root, loop_id) / ".lock"
