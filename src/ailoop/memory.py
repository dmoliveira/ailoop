from __future__ import annotations

import fcntl
import getpass
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal

from .config import resolve_run_config
from .models import AppConfig, LoopRunConfig, utc_now
from .paths import ensure_dir

MemoryKind = Literal["preset", "history"]
MEMORY_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")
MAX_MEMORY_ID_ATTEMPTS = 32

EDITABLE_SNAPSHOT_FIELDS = frozenset(
    {
        "prompt",
        "runner",
        "agent",
        "steps",
        "pause_seconds",
        "task_file",
        "until_tasks_complete",
        "no_pre_prompt",
        "no_agent_file",
        "agent_file",
        "workspace_root",
        "workspace_history_enabled",
        "workspace_history_limit",
        "workspace_history_chars",
        "extra_args",
    }
)


def _known_fields(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    names = {item.name for item in fields(cls)}
    return {name: value for name, value in data.items() if name in names}


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
    workspace_root: str | None = None
    workspace_history_enabled: bool = True
    workspace_history_limit: int = 5
    workspace_history_chars: int = 1200
    extra_args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SnapshotValidator = Callable[[VersionSnapshot], None]


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
        scope = MemoryScope(**_known_fields(MemoryScope, data["scope"]))
        current = VersionSnapshot(**_known_fields(VersionSnapshot, data["current"]))
        versions = [
            VersionSnapshot(**_known_fields(VersionSnapshot, item)) for item in data["versions"]
        ]
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
        workspace_root=run_config.workspace_root,
        workspace_history_enabled=run_config.workspace_history_enabled,
        workspace_history_limit=run_config.workspace_history_limit,
        workspace_history_chars=run_config.workspace_history_chars,
    )


class MemoryStore:
    def __init__(self, state_root: Path):
        self.root = self._ensure_storage_directory(state_root / "memory")
        self.presets_dir = self._ensure_storage_directory(self.root / "presets")
        self.history_dir = self._ensure_storage_directory(self.root / "history")
        self.locks_dir = self._ensure_storage_directory(self.root / "locks")

    @staticmethod
    def _ensure_storage_directory(path: Path) -> Path:
        try:
            observed = path.lstat()
        except FileNotFoundError:
            ensure_dir(path)
            observed = path.lstat()
        if not stat.S_ISDIR(observed.st_mode):
            raise RuntimeError(f"Memory storage path is not a real directory: {path}")
        return path

    def _kind_dir(self, kind: MemoryKind) -> Path:
        return self.presets_dir if kind == "preset" else self.history_dir

    def _entry_path(self, kind: MemoryKind, entry_id: str) -> Path:
        self._validate_entry_id(entry_id)
        return self._kind_dir(kind) / f"{entry_id}.json"

    @staticmethod
    def _validate_entry_id(entry_id: str) -> None:
        if MEMORY_ID_PATTERN.fullmatch(entry_id) is None:
            raise ValueError(f"Invalid memory entry id: {entry_id}")

    @contextmanager
    def _entry_lock(self, entry_id: str) -> Iterator[None]:
        self._validate_entry_id(entry_id)
        lock_path = self.locks_dir / f"{entry_id}.lock"
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise RuntimeError("Secure no-follow memory locking is unavailable")
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | no_follow | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise RuntimeError(f"Memory entry lock is not a regular file: {entry_id}")
            if hasattr(os, "getuid") and observed.st_uid != os.getuid():
                raise RuntimeError(f"Memory entry lock has an unexpected owner: {entry_id}")
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @staticmethod
    def _path_occupied(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _read_entry_bytes(path: Path) -> bytes:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise RuntimeError("Secure no-follow memory reads are unavailable")
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(f"Memory entry path is unsafe: {path.name}") from exc
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise ValueError(f"Memory entry is not a private regular file: {path.name}")
            if hasattr(os, "getuid") and observed.st_uid != os.getuid():
                raise ValueError(f"Memory entry has an unexpected owner: {path.name}")
            if stat.S_IMODE(observed.st_mode) & 0o077:
                raise ValueError(f"Memory entry permissions are not private: {path.name}")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _publish_new_unlocked(self, entry: MemoryEntry) -> None:
        path = self._entry_path(entry.kind, entry.id)
        payload = json.dumps(entry.to_dict(), indent=2)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        published = False
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary_path, path, follow_symlinks=False)
            published = True
            try:
                temporary_path.unlink()
            except OSError as cleanup_error:
                published_stat = temporary_path.stat()
                try:
                    observed = path.lstat()
                    if (observed.st_dev, observed.st_ino) == (
                        published_stat.st_dev,
                        published_stat.st_ino,
                    ):
                        path.unlink()
                finally:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError as retry_error:
                        cleanup_error.add_note(
                            f"failed to clean memory staging file: {retry_error}"
                        )
                    self._fsync_directory(path.parent)
                raise
            observed = path.lstat()
            if observed.st_nlink != 1:
                path.unlink(missing_ok=True)
                self._fsync_directory(path.parent)
                raise RuntimeError(f"Published memory entry is not private: {entry.id}")
            self._fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not published:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _write_unlocked(self, entry: MemoryEntry) -> None:
        path = self._entry_path(entry.kind, entry.id)
        payload = json.dumps(entry.to_dict(), indent=2)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            self._fsync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        # File contents are fsynced before publication. Once metadata publication has
        # committed, do not report an ambiguous failure that could invite a duplicate retry.
        try:
            directory = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(directory)
            except OSError:
                pass
        finally:
            os.close(directory)

    def save(self, entry: MemoryEntry) -> None:
        with self._entry_lock(entry.id):
            if any(
                self._path_occupied(self._entry_path(kind, entry.id))
                for kind in ("preset", "history")
            ):
                raise FileExistsError(f"Memory entry id already exists: {entry.id}")
            self._publish_new_unlocked(entry)

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
        try:
            self._validate_entry_id(entry_id)
        except ValueError:
            raise FileNotFoundError(f"Memory entry not found: {entry_id}") from None
        for kind in ("preset", "history"):
            path = self._entry_path(kind, entry_id)  # type: ignore[arg-type]
            if self._path_occupied(path):
                entry = MemoryEntry.from_dict(json.loads(self._read_entry_bytes(path)))
                if entry.id != entry_id or entry.kind != kind:
                    raise ValueError(f"Memory entry identity differs from its path: {entry_id}")
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
        include_archived: bool = False,
        labels: list[str] | None = None,
        query: str | None = None,
        all_folders: bool = False,
        folder: Path | None = None,
        user_id: str | None = None,
    ) -> list[MemoryEntry]:
        dirs = (
            [(kind, self._kind_dir(kind))]
            if kind
            else [("preset", self.presets_dir), ("history", self.history_dir)]
        )
        entries: list[MemoryEntry] = []
        required_labels = set(labels or [])
        query_text = (query or "").strip().lower()
        folder_path = str(folder.expanduser().resolve()) if folder else None
        actual_user = user_id or current_user_id()
        for expected_kind, directory in dirs:
            for path in sorted(directory.glob("*.json")):
                try:
                    entry = MemoryEntry.from_dict(json.loads(self._read_entry_bytes(path)))
                    if entry.id != path.stem or entry.kind != expected_kind:
                        raise ValueError("Memory entry identity differs from its path")
                except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                if entry.archived and not include_archived:
                    continue
                if favorites_only and not entry.favorite:
                    continue
                if required_labels and not required_labels.issubset(set(entry.labels)):
                    continue
                if query_text:
                    haystack = " ".join([entry.id, entry.title, *entry.labels]).lower()
                    if query_text not in haystack:
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
        for _attempt in range(MAX_MEMORY_ID_ATTEMPTS):
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
                current=deepcopy(snapshot),
                versions=[deepcopy(snapshot)],
            )
            try:
                self.save(entry)
            except FileExistsError:
                continue
            return entry
        raise RuntimeError(
            f"Unable to allocate a unique memory entry id after {MAX_MEMORY_ID_ATTEMPTS} attempts"
        )

    def edit(
        self,
        entry_id: str,
        *,
        run_config: LoopRunConfig | None = None,
        snapshot_updates: Mapping[str, object] | None = None,
        snapshot_validator: SnapshotValidator | None = None,
        title: str | None = None,
        labels: list[str] | None = None,
        favorite: bool | None = None,
        archived: bool | None = None,
        change_note: str | None = None,
        token_ref: str | None = None,
        folder: Path | None = None,
        user_id: str | None = None,
        all_folders: bool = False,
    ) -> MemoryEntry:
        if run_config is not None and snapshot_updates is not None:
            raise ValueError("run_config and snapshot_updates are mutually exclusive")
        updates = dict(snapshot_updates or {})
        unknown = set(updates) - EDITABLE_SNAPSHOT_FIELDS
        if unknown:
            raise ValueError(f"Unsupported memory snapshot fields: {', '.join(sorted(unknown))}")
        if change_note is not None and run_config is None and not updates:
            raise ValueError("change_note requires a saved configuration change")
        if (
            run_config is None
            and not updates
            and title is None
            and labels is None
            and favorite is None
            and archived is None
        ):
            raise ValueError("No memory changes requested")

        with self._entry_lock(entry_id):
            entry = self.load(
                entry_id,
                folder=folder,
                user_id=user_id,
                all_folders=all_folders,
            )
            actual_user = user_id or current_user_id()
            entry.updated_at = utc_now()
            entry.updated_by = actual_user
            if title is not None:
                entry.title = title
            if labels is not None:
                entry.labels = labels
            if favorite is not None:
                entry.favorite = favorite
            if archived is not None:
                entry.archived = archived
            if run_config is not None or updates:
                version = entry.latest_version + 1
                if run_config is not None:
                    snapshot = snapshot_from_run_config(
                        run_config,
                        version=version,
                        saved_by=actual_user,
                        token_ref=token_ref,
                        change_note=change_note,
                    )
                else:
                    if "extra_args" in updates:
                        updates["extra_args"] = list(updates["extra_args"])  # type: ignore[arg-type]
                    snapshot = replace(
                        deepcopy(entry.current),
                        **updates,
                        version=version,
                        saved_at=utc_now(),
                        saved_by=actual_user,
                        token_ref=(entry.current.token_ref if token_ref is None else token_ref),
                        change_note=change_note,
                    )
                if snapshot_validator is not None:
                    snapshot_validator(snapshot)
                entry.token_ref = snapshot.token_ref
                entry.latest_version = version
                entry.current = deepcopy(snapshot)
                entry.versions.append(deepcopy(snapshot))
            self._write_unlocked(entry)
            return entry

    def delete(
        self,
        entry_id: str,
        *,
        folder: Path | None = None,
        user_id: str | None = None,
        all_folders: bool = False,
    ) -> None:
        with self._entry_lock(entry_id):
            entry = self.load(entry_id, folder=folder, user_id=user_id, all_folders=all_folders)
            path = self._entry_path(entry.kind, entry.id)
            path.unlink(missing_ok=True)
            self._fsync_directory(path.parent)

    def mark_used(
        self,
        entry_id: str,
        *,
        folder: Path | None = None,
        user_id: str | None = None,
        all_folders: bool = False,
    ) -> MemoryEntry:
        with self._entry_lock(entry_id):
            entry = self.load(entry_id, folder=folder, user_id=user_id, all_folders=all_folders)
            entry.use_count += 1
            entry.last_used_at = utc_now()
            self._write_unlocked(entry)
            return entry


def render_memory_list(entries: list[MemoryEntry], *, all_folders: bool = False) -> str:
    if not entries:
        scope_text = "all folders" if all_folders else "current folder"
        next_filter = "memory list --all-folders" if not all_folders else "memory save"
        return "\n".join(
            [
                "No memory entries found.",
                "",
                f"scope: {scope_text}",
                "Try:",
                '  ailoop memory save "Quick review" "Review the repo"',
                f"  ailoop {next_filter}" if not all_folders else "  ailoop memory list",
            ]
        )
    lines = [
        "ID             Kind     Fav  Used  Labels       Title",
        "-------------  -------  ---  ----  -----------  -----",
    ]
    for entry in entries:
        star = "★" if entry.favorite else "-"
        labels = ",".join(entry.labels[:2]) or "-"
        lines.append(
            f"{entry.id:<13}  {entry.kind:<7}  {star:<3}  {entry.use_count:<4}  "
            f"{labels:<11}  {entry.title}"
        )
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
        workspace_root=current.workspace_root or entry.scope.folder_path,
        workspace_history_enabled=current.workspace_history_enabled,
        workspace_history_limit=current.workspace_history_limit,
        workspace_history_chars=current.workspace_history_chars,
    )
