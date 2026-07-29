from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import LoopState, utc_now
from .paths import (
    ensure_dir,
    events_file,
    lock_file,
    mutation_lock_file,
    raw_loop_dir,
    retired_file,
    state_file,
    validate_loop_id,
)


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _atomic_write(path: Path, payload: str) -> None:
    ensure_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


class StateStore:
    def __init__(self, state_root: Path):
        self.state_root = ensure_dir(state_root)

    def _write_unlocked(self, state: LoopState) -> None:
        state.updated_at = utc_now()
        path = state_file(self.state_root, state.loop_id)
        payload = json.dumps(state.to_dict(), indent=2)
        _atomic_write(path, payload)

    def create_if_absent(self, state: LoopState) -> None:
        validate_loop_id(state.loop_id)
        with self.acquire_mutation_lock(state.loop_id):
            if self.is_retired(state.loop_id):
                raise RuntimeError(
                    f"Loop id has been retired and cannot be reused: {state.loop_id}"
                )
            directory = raw_loop_dir(self.state_root, state.loop_id)
            try:
                directory.mkdir()
            except FileExistsError as exc:
                raise RuntimeError(f"Loop already exists: {state.loop_id}") from exc
            try:
                self._write_unlocked(state)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise

    def load(self, loop_id: str) -> LoopState:
        validate_loop_id(loop_id)
        path = state_file(self.state_root, loop_id)
        if not path.exists():
            raise FileNotFoundError(f"Loop state not found: {loop_id}")
        return LoopState.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_states(self) -> list[LoopState]:
        states: list[LoopState] = []
        for child in sorted(self.state_root.iterdir()):
            if not child.is_dir():
                continue
            try:
                validate_loop_id(child.name)
            except ValueError:
                continue
            path = child / "state.json"
            if not path.exists():
                continue
            try:
                states.append(LoopState.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        return sorted(states, key=lambda item: item.updated_at, reverse=True)

    @contextmanager
    def acquire_mutation_lock(self, loop_id: str) -> Iterator[None]:
        path = mutation_lock_file(self.state_root, loop_id)
        ensure_dir(path.parent)
        handle = path.open("a", encoding="utf-8")
        acquired = False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired = True
            yield
        finally:
            # The protected operation may already have committed. Preserve its
            # result or primary exception while still attempting both cleanup steps.
            if acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                handle.close()
            except OSError:
                pass

    @contextmanager
    def mutate(self, loop_id: str) -> Iterator[LoopState]:
        with self.acquire_mutation_lock(loop_id):
            if self.is_retired(loop_id):
                raise RuntimeError(f"Loop has been retired: {loop_id}")
            state = self.load(loop_id)
            yield state
            if state.loop_id != loop_id:
                raise ValueError("Loop id cannot be changed during a state mutation")
            self._write_unlocked(state)

    def is_retired(self, loop_id: str) -> bool:
        return retired_file(self.state_root, loop_id).is_file()

    def retirement_record(self, loop_id: str) -> dict[str, str] | None:
        path = retired_file(self.state_root, loop_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"loop_id": loop_id, "tombstone": ""}
        if not isinstance(payload, dict):
            return {"loop_id": loop_id, "tombstone": ""}
        return {
            "loop_id": str(payload.get("loop_id", loop_id)),
            "tombstone": str(payload.get("tombstone", "")),
        }

    def reserve_retirement(self, loop_id: str, tombstone: str) -> None:
        """Durably retire an id. The caller must hold its mutation lock."""
        path = retired_file(self.state_root, loop_id)
        if path.exists():
            return
        _atomic_write(
            path,
            json.dumps(
                {
                    "loop_id": loop_id,
                    "tombstone": tombstone,
                    "retired_at": utc_now(),
                },
                indent=2,
            ),
        )

    def is_locked(self, loop_id: str) -> bool:
        path = lock_file(self.state_root, loop_id)
        ensure_dir(path.parent)
        with path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            try:
                return False
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def append_event(self, loop_id: str, event: dict) -> bool:
        with self.acquire_mutation_lock(loop_id):
            path = events_file(self.state_root, loop_id)
            if self.is_retired(loop_id) or not state_file(self.state_root, loop_id).is_file():
                return False
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(path.parent)
            return True

    @contextmanager
    def acquire_lock(self, loop_id: str) -> Iterator[None]:
        path = lock_file(self.state_root, loop_id)
        ensure_dir(path.parent)
        handle = path.open("a+", encoding="utf-8")
        acquired = False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"Loop is already active: {loop_id}") from exc
            acquired = True
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
            yield
        finally:
            if acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                handle.close()
            except OSError:
                pass
