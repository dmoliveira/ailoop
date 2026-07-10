from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .models import IterationRecord, LoopRunConfig, utc_now
from .paths import ensure_dir, workspace_history_file

WorkspaceHistoryKind = Literal["prompt", "follow_up", "result"]


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
        return cls(**data)


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

    def append(self, entry: WorkspaceHistoryEntry) -> None:
        path = workspace_history_file(self.state_root, entry.workspace_root)
        ensure_dir(path.parent)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict()) + "\n")

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

    def latest_prompt(self, workspace_root: str | None) -> str | None:
        root = canonical_workspace_root(workspace_root)
        if not root:
            return None
        path = workspace_history_file(self.state_root, root)
        if not path.exists():
            return None
        for raw_line in reversed(path.read_text(encoding="utf-8").splitlines()):
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
        path = workspace_history_file(self.state_root, root)
        if not path.exists():
            return []
        rows: list[WorkspaceHistoryEntry] = []
        total_chars = 0
        for raw_line in reversed(path.read_text(encoding="utf-8").splitlines()):
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
