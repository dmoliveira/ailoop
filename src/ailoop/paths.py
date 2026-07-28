from __future__ import annotations

import hashlib
import re
from collections import deque
from pathlib import Path

LOOP_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?\Z")
RESERVED_LOOP_IDS = {"locks", "retired", "trash", "workspaces"}


def validate_loop_id(loop_id: str) -> str:
    if not LOOP_ID_PATTERN.fullmatch(loop_id) or loop_id in RESERVED_LOOP_IDS:
        raise ValueError(
            "Loop id must be 1-64 lowercase letters, numbers, hyphens, or underscores "
            "and cannot be a reserved name"
        )
    return loop_id


def _loop_key(loop_id: str) -> str:
    validate_loop_id(loop_id)
    return hashlib.sha256(loop_id.encode()).hexdigest()


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
    validate_loop_id(loop_id)
    return state_root / loop_id


def loop_dir(state_root: Path, loop_id: str) -> Path:
    return raw_loop_dir(state_root, loop_id)


def state_file(state_root: Path, loop_id: str) -> Path:
    return raw_loop_dir(state_root, loop_id) / "state.json"


def events_file(state_root: Path, loop_id: str) -> Path:
    return raw_loop_dir(state_root, loop_id) / "events.jsonl"


def log_dir(state_root: Path, loop_id: str) -> Path:
    return raw_loop_dir(state_root, loop_id) / "logs"


def lock_file(state_root: Path, loop_id: str) -> Path:
    return state_root / ".locks" / f"{_loop_key(loop_id)}.execution.lock"


def mutation_lock_file(state_root: Path, loop_id: str) -> Path:
    return state_root / ".locks" / f"{_loop_key(loop_id)}.mutation.lock"


def retired_file(state_root: Path, loop_id: str) -> Path:
    return state_root / ".retired" / f"{_loop_key(loop_id)}.json"


def trash_dir(state_root: Path) -> Path:
    return state_root / ".trash"


def workspace_history_dir(state_root: Path, workspace_root: str) -> Path:
    digest = hashlib.sha256(workspace_root.encode()).hexdigest()[:16]
    return state_root / "workspaces" / digest


def workspace_history_file(state_root: Path, workspace_root: str) -> Path:
    return workspace_history_dir(state_root, workspace_root) / "prompt-history.jsonl"
