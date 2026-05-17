from __future__ import annotations

import getpass
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import resolve_run_config
from .models import AppConfig, LoopRunConfig, utc_now
from .paths import ensure_dir

MemoryKind = Literal["preset", "history"]


@dataclass(slots=True)
class MemoryScope:
    user_id: str
    user_label: str | None
    folder_path: str
    folder_hash: str
    scope_key: str
    is_global: bool = False


@dataclass(slots=True)
class VersionSnapshot:
    version: int
    saved_at: str
    saved_by: str
    token_ref: str | None
    change_note: str | None
    command_name: str
    prompt: str
    runner: str | None
    agent: str | None
    steps: int | None
    pause_seconds: int | None
    task_file: str | None
    until_tasks_complete: bool
    no_pre_prompt: bool
    no_agent_file: bool
    agent_file: str | None
    extra_args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MemoryEntry:
    id: str
    kind: MemoryKind
    scope: MemoryScope
    title: str
    labels: list[str]
    favorite: bool
    archived: bool
    created_at: str
    created_by: str
    updated_at: str
    updated_by: str
    last_used_at: str | None
    use_count: int
    token_ref: str | None
    source_loop_id: str | None
    source_command: str | None
    latest_version: int
    current: VersionSnapshot
    versions: list[VersionSnapshot]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["current"] = self.current.to_dict()
        payload["versions"] = [item.to_dict() for item in self.versions]
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        scope = MemoryScope(**data["scope"])
        current = VersionSnapshot(**data["current"])
        versions = [VersionSnapshot(**item) for item in data["versions"]]
        return cls(
            id=data["id"],
            kind=data["kind"],
            scope=scope,
            title=data["title"],
            labels=list(data.get("labels", [])),
            favorite=bool(data.get("favorite", False)),
            archived=bool(data.get("archived", False)),
            created_at=data["created_at"],
            created_by=data["created_by"],
            updated_at=data["updated_at"],
            updated_by=data["updated_by"],
            last_used_at=data.get("last_used_at"),
            use_count=int(data.get("use_count", 0)),
            token_ref=data.get("token_ref"),
            source_loop_id=data.get("source_loop_id"),
            source_command=data.get("source_command"),
            latest_version=int(data.get("latest_version", 1)),
            current=current,
            versions=versions,
        )


def current_user_id() -> str:
    return getpass.getuser()


def build_scope(
    folder: Path,
    user_id: str | None = None,
    user_label: str | None = None,
) -> MemoryScope:
    resolved = folder.expanduser().resolve()
    actual_user = user_id or current_user_id()
    folder_hash = hashlib.sha1(str(resolved).encode()).hexdigest()[:12]
    return MemoryScope(
        user_id=actual_user,
        user_label=user_label,
        folder_path=str(resolved),
        folder_hash=folder_hash,
        scope_key=f"{actual_user}:{folder_hash}",
    )


def snapshot_from_run_config(
    run_config: LoopRunConfig,
    *,
    version: int,
    saved_by: str,
    token_ref: str | None = None,
    change_note: str | None = None,
) -> VersionSnapshot:
    return VersionSnapshot(
        version=version,
        saved_at=utc_now(),
        saved_by=saved_by,
        token_ref=token_ref,
        change_note=change_note,
        command_name="run",
        prompt=run_config.prompt,
        runner=run_config.runner,
        agent=run_config.agent,
        steps=run_config.steps,
        pause_seconds=run_config.pause_seconds,
        task_file=run_config.task_file,
        until_tasks_complete=run_config.stop_when_tasks_complete,
        no_pre_prompt=not run_config.pre_prompt_enabled,
        no_agent_file=not run_config.attach_agent_file,
        agent_file=run_config.agent_file,
    )


class MemoryStore:
    def __init__(self, state_root: Path):
        self.root = ensure_dir(state_root / "memory")
        self.presets_dir = ensure_dir(self.root / "presets")
        self.history_dir = ensure_dir(self.root / "history")

    def _kind_dir(self, kind: MemoryKind) -> Path:
        return self.presets_dir if kind == "preset" else self.history_dir

    def _entry_path(self, kind: MemoryKind, entry_id: str) -> Path:
        return self._kind_dir(kind) / f"{entry_id}.json"

    def save(self, entry: MemoryEntry) -> None:
        self._entry_path(entry.kind, entry.id).write_text(json.dumps(entry.to_dict(), indent=2))

    def _authorize(
        self,
        entry: MemoryEntry,
        *,
        folder: Path | None = None,
        user_id: str | None = None,
        all_folders: bool = False,
    ) -> MemoryEntry:
        actual_user = user_id or current_user_id()
        if entry.scope.user_id != actual_user:
            raise FileNotFoundError(f"Memory entry not found: {entry.id}")
        if folder is not None and not all_folders:
            folder_path = str(folder.expanduser().resolve())
            if entry.scope.folder_path != folder_path:
                raise FileNotFoundError(f"Memory entry not found: {entry.id}")
        return entry

    def load(
        self,
        entry_id: str,
        *,
        folder: Path | None = None,
        user_id: str | None = None,
        all_folders: bool = False,
    ) -> MemoryEntry:
        for kind in ("preset", "history"):
            path = self._entry_path(kind, entry_id)  # type: ignore[arg-type]
            if path.exists():
                entry = MemoryEntry.from_dict(json.loads(path.read_text()))
                return self._authorize(
                    entry,
                    folder=folder,
                    user_id=user_id,
                    all_folders=all_folders,
                )
        raise FileNotFoundError(f"Memory entry not found: {entry_id}")

    def list_entries(
        self,
        *,
        kind: MemoryKind | None = None,
        favorites_only: bool = False,
        all_folders: bool = False,
        folder: Path | None = None,
        user_id: str | None = None,
    ) -> list[MemoryEntry]:
        dirs = [self._kind_dir(kind)] if kind else [self.presets_dir, self.history_dir]
        entries: list[MemoryEntry] = []
        folder_path = str(folder.expanduser().resolve()) if folder else None
        actual_user = user_id or current_user_id()
        for directory in dirs:
            for path in sorted(directory.glob("*.json")):
                entry = MemoryEntry.from_dict(json.loads(path.read_text()))
                if favorites_only and not entry.favorite:
                    continue
                if entry.scope.user_id != actual_user:
                    continue
                if not all_folders and folder_path and entry.scope.folder_path != folder_path:
                    continue
                entries.append(entry)
        return sorted(entries, key=lambda item: item.updated_at, reverse=True)

    def create(
        self,
        *,
        kind: MemoryKind,
        title: str,
        run_config: LoopRunConfig,
        folder: Path,
        labels: list[str] | None = None,
        favorite: bool = False,
        user_id: str | None = None,
        user_label: str | None = None,
        token_ref: str | None = None,
        source_loop_id: str | None = None,
        source_command: str | None = None,
    ) -> MemoryEntry:
        actual_user = user_id or current_user_id()
        scope = build_scope(folder, user_id=actual_user, user_label=user_label)
        snapshot = snapshot_from_run_config(
            run_config,
            version=1,
            saved_by=actual_user,
            token_ref=token_ref,
        )
        now = utc_now()
        entry = MemoryEntry(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            scope=scope,
            title=title,
            labels=labels or [],
            favorite=favorite,
            archived=False,
            created_at=now,
            created_by=actual_user,
            updated_at=now,
            updated_by=actual_user,
            last_used_at=None,
            use_count=0,
            token_ref=token_ref,
            source_loop_id=source_loop_id,
            source_command=source_command,
            latest_version=1,
            current=snapshot,
            versions=[snapshot],
        )
        self.save(entry)
        return entry

    def edit(
        self,
        entry_id: str,
        *,
        run_config: LoopRunConfig | None = None,
        title: str | None = None,
        labels: list[str] | None = None,
        favorite: bool | None = None,
        change_note: str | None = None,
        token_ref: str | None = None,
        folder: Path | None = None,
        user_id: str | None = None,
        all_folders: bool = False,
    ) -> MemoryEntry:
        entry = self.load(entry_id, folder=folder, user_id=user_id, all_folders=all_folders)
        actual_user = user_id or current_user_id()
        entry.updated_at = utc_now()
        entry.updated_by = actual_user
        if title is not None:
            entry.title = title
        if labels is not None:
            entry.labels = labels
        if favorite is not None:
            entry.favorite = favorite
        if run_config is not None:
            version = entry.latest_version + 1
            snapshot = snapshot_from_run_config(
                run_config,
                version=version,
                saved_by=actual_user,
                token_ref=token_ref,
                change_note=change_note,
            )
            entry.latest_version = version
            entry.current = snapshot
            entry.versions.append(snapshot)
        self.save(entry)
        return entry

    def delete(
        self,
        entry_id: str,
        *,
        folder: Path | None = None,
        user_id: str | None = None,
        all_folders: bool = False,
    ) -> None:
        entry = self.load(entry_id, folder=folder, user_id=user_id, all_folders=all_folders)
        self._entry_path(entry.kind, entry.id).unlink(missing_ok=True)

    def mark_used(
        self,
        entry_id: str,
        *,
        folder: Path | None = None,
        user_id: str | None = None,
        all_folders: bool = False,
    ) -> MemoryEntry:
        entry = self.load(entry_id, folder=folder, user_id=user_id, all_folders=all_folders)
        entry.use_count += 1
        entry.last_used_at = utc_now()
        self.save(entry)
        return entry


def render_memory_list(entries: list[MemoryEntry]) -> str:
    if not entries:
        return "No memory entries found."
    lines = [
        "ID             Kind     Fav  Title",
        "-------------  -------  ---  -----",
    ]
    for entry in entries:
        star = "★" if entry.favorite else "-"
        lines.append(f"{entry.id:<13}  {entry.kind:<7}  {star:<3}  {entry.title}")
    return "\n".join(lines)


def render_memory_show(entry: MemoryEntry) -> str:
    return "\n".join(
        [
            f"id: {entry.id}",
            f"kind: {entry.kind}",
            f"title: {entry.title}",
            f"favorite: {entry.favorite}",
            f"labels: {', '.join(entry.labels) or '-'}",
            f"folder: {entry.scope.folder_path}",
            f"created_by: {entry.created_by}",
            f"updated_by: {entry.updated_by}",
            f"versions: {entry.latest_version}",
            f"runner: {entry.current.runner}",
            f"agent: {entry.current.agent}",
            f"steps: {entry.current.steps}",
            f"pause_seconds: {entry.current.pause_seconds}",
            f"task_file: {entry.current.task_file or '-'}",
            f"prompt: {entry.current.prompt}",
        ]
    )


def run_config_from_entry(entry: MemoryEntry, app_config: AppConfig) -> LoopRunConfig:
    current = entry.current
    return resolve_run_config(
        app_config,
        prompt=current.prompt,
        runner=current.runner,
        agent=current.agent,
        steps=current.steps,
        pause_seconds=current.pause_seconds,
        pre_prompt_enabled=False if current.no_pre_prompt else None,
        attach_agent_file=False if current.no_agent_file else None,
        agent_file=current.agent_file,
        task_file=current.task_file,
        stop_when_tasks_complete=current.until_tasks_complete,
    )
