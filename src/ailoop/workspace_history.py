from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal

from .models import IterationRecord, LoopRunConfig, utc_now
from .paths import ensure_dir, read_last_lines, workspace_history_file

WorkspaceHistoryKind = Literal["prompt", "follow_up", "result"]
HISTORY_TAIL_LINES = 400
HISTORY_MAX_BYTES = 2_000_000
HISTORY_RETAIN_LINES = 1_000


@dataclass(slots=True)
class WorkspaceHistoryEntry:
    recorded_at: str
    workspace_root: str
    workspace_hash: str
    loop_id: str
    kind: WorkspaceHistoryKind
    prompt: str | None = None
    summary: str | None = None
    iteration: int | None = None
    exit_code: int | None = None
    prompt_file: str | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> WorkspaceHistoryEntry:
        names = {item.name for item in fields(cls)}
        return cls(**{name: value for name, value in data.items() if name in names})


def canonical_workspace_root(value: str | None) -> str | None:
    if not value:
        return None
    return str(Path(value).expanduser().resolve())


def workspace_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def workspace_prompt_signature(workspace_root: str | None, prompt: str) -> str | None:
    root = canonical_workspace_root(workspace_root)
    if not root:
        return None
    return hashlib.sha256(f"{root}\0{prompt.strip()}".encode()).hexdigest()


class WorkspaceHistoryStore:
    def __init__(self, state_root: Path):
        self.state_root = state_root

    def _history_path(self, workspace_root: str, *, create: bool) -> Path:
        path = workspace_history_file(self.state_root, workspace_root)
        root = self.state_root / "workspaces"
        if create:
            ensure_dir(root)
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise RuntimeError(f"Unsafe workspace history root: {root}")
        if create:
            path.parent.mkdir(mode=0o700, exist_ok=True)
        if path.parent.is_symlink() or (path.parent.exists() and not path.parent.is_dir()):
            raise RuntimeError(f"Unsafe workspace history directory: {path.parent}")
        if path.parent.exists() and path.parent.resolve().parent != root.resolve():
            raise RuntimeError(f"Workspace history directory escapes root: {path.parent}")
        if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
            raise RuntimeError(f"Unsafe workspace history file: {path}")
        return path

    def _validate_discovered_history_path(self, path: Path) -> bool:
        root = self.state_root / "workspaces"
        return (
            not root.is_symlink()
            and not path.parent.is_symlink()
            and not path.is_symlink()
            and path.is_file()
            and path.parent.resolve().parent == root.resolve()
        )

    def append(self, entry: WorkspaceHistoryEntry) -> None:
        path = self._history_path(entry.workspace_root, create=True)
        lock_path = path.parent / "prompt-history.lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, flags, 0o600)
        with os.fdopen(lock_fd, "a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                opened = os.fstat(lock_handle.fileno())
                linked = os.stat(lock_path, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (
                    linked.st_dev,
                    linked.st_ino,
                ) or opened.st_nlink != 1:
                    raise RuntimeError(f"Workspace history lock identity changed: {lock_path}")
                data_flags = (
                    os.O_WRONLY
                    | os.O_APPEND
                    | os.O_CREAT
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                data_fd = os.open(path, data_flags, 0o600)
                with os.fdopen(data_fd, "a", encoding="utf-8") as handle:
                    opened_data = os.fstat(handle.fileno())
                    linked_data = os.stat(path, follow_symlinks=False)
                    if (
                        (opened_data.st_dev, opened_data.st_ino)
                        != (linked_data.st_dev, linked_data.st_ino)
                        or opened_data.st_nlink != 1
                        or not stat.S_ISREG(opened_data.st_mode)
                    ):
                        raise RuntimeError(f"Workspace history file identity changed: {path}")
                    handle.write(json.dumps(entry.to_dict()) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if path.stat().st_size > HISTORY_MAX_BYTES:
                    content = read_last_lines(path, HISTORY_RETAIN_LINES) + "\n"
                    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as temp_handle:
                            temp_handle.write(content)
                            temp_handle.flush()
                            os.fsync(temp_handle.fileno())
                        os.replace(temp_name, path)
                        directory_fd = os.open(path.parent, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                    finally:
                        if os.path.exists(temp_name):
                            os.unlink(temp_name)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def append_prompt(self, loop_id: str, run_config: LoopRunConfig) -> None:
        root = canonical_workspace_root(run_config.workspace_root)
        if not root:
            return
        self.append(
            WorkspaceHistoryEntry(
                recorded_at=utc_now(),
                workspace_root=root,
                workspace_hash=workspace_hash(root),
                loop_id=loop_id,
                kind="prompt",
                prompt=run_config.prompt.strip() or None,
            )
        )

    def append_follow_up(self, workspace_root: str | None, loop_id: str, follow_up: str) -> None:
        root = canonical_workspace_root(workspace_root)
        if not root or not follow_up.strip():
            return
        self.append(
            WorkspaceHistoryEntry(
                recorded_at=utc_now(),
                workspace_root=root,
                workspace_hash=workspace_hash(root),
                loop_id=loop_id,
                kind="follow_up",
                prompt=follow_up.strip(),
            )
        )

    def append_result(
        self,
        workspace_root: str | None,
        loop_id: str,
        iteration: IterationRecord,
    ) -> None:
        root = canonical_workspace_root(workspace_root)
        if not root:
            return
        self.append(
            WorkspaceHistoryEntry(
                recorded_at=utc_now(),
                workspace_root=root,
                workspace_hash=workspace_hash(root),
                loop_id=loop_id,
                kind="result",
                summary=iteration.summary,
                iteration=iteration.number,
                exit_code=iteration.exit_code,
                prompt_file=iteration.prompt_file,
                stdout_log=iteration.stdout_log,
                stderr_log=iteration.stderr_log,
            )
        )

    def recent_workspace_roots(self, limit: int = 12) -> list[str]:
        if limit <= 0:
            return []
        roots: list[str] = []
        history_files = sorted(
            (
                path
                for path in (self.state_root / "workspaces").glob("*/prompt-history.jsonl")
                if self._validate_discovered_history_path(path)
            ),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for path in history_files:
            for raw_line in reversed(read_last_lines(path, HISTORY_TAIL_LINES).splitlines()):
                try:
                    entry = WorkspaceHistoryEntry.from_dict(json.loads(raw_line))
                except (json.JSONDecodeError, TypeError):
                    continue
                if entry.workspace_root not in roots:
                    roots.append(entry.workspace_root)
                break
            if len(roots) >= limit:
                break
        return roots

    def latest_prompt(self, workspace_root: str | None) -> str | None:
        root = canonical_workspace_root(workspace_root)
        if not root:
            return None
        path = self._history_path(root, create=False)
        if not path.exists():
            return None
        for raw_line in reversed(read_last_lines(path, HISTORY_TAIL_LINES).splitlines()):
            try:
                entry = WorkspaceHistoryEntry.from_dict(json.loads(raw_line))
            except (json.JSONDecodeError, TypeError):
                continue
            if entry.kind == "prompt":
                return entry.prompt
        return None

    def recent_entries(
        self,
        workspace_root: str | None,
        *,
        limit: int = 5,
        max_chars: int = 1200,
    ) -> list[WorkspaceHistoryEntry]:
        root = canonical_workspace_root(workspace_root)
        if not root or limit <= 0 or max_chars <= 0:
            return []
        path = self._history_path(root, create=False)
        if not path.exists():
            return []
        rows: list[WorkspaceHistoryEntry] = []
        total_chars = 0
        for raw_line in reversed(read_last_lines(path, HISTORY_TAIL_LINES).splitlines()):
            if not raw_line.strip():
                continue
            try:
                entry = WorkspaceHistoryEntry.from_dict(json.loads(raw_line))
            except (json.JSONDecodeError, TypeError):
                continue
            text = entry.prompt or entry.summary or ""
            total_chars += len(text)
            if total_chars > max_chars and rows:
                break
            rows.append(entry)
            if len(rows) >= limit:
                break
        rows.reverse()
        return rows
