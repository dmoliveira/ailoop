from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import LoopState, utc_now
from .paths import ensure_dir, events_file, lock_file, state_file


def _atomic_write(path: Path, payload: str) -> None:
    ensure_dir(path.parent)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(payload)
    temp_path.replace(path)


class StateStore:
    def __init__(self, state_root: Path):
        self.state_root = ensure_dir(state_root)

    def save(self, state: LoopState) -> None:
        state.updated_at = utc_now()
        path = state_file(self.state_root, state.loop_id)
        payload = json.dumps(state.to_dict(), indent=2)
        _atomic_write(path, payload)

    def load(self, loop_id: str) -> LoopState:
        path = state_file(self.state_root, loop_id)
        if not path.exists():
            raise FileNotFoundError(f"Loop state not found: {loop_id}")
        return LoopState.from_dict(json.loads(path.read_text()))

    def list_states(self) -> list[LoopState]:
        states: list[LoopState] = []
        for child in sorted(self.state_root.iterdir()):
            if not child.is_dir():
                continue
            path = child / "state.json"
            if not path.exists():
                continue
            states.append(LoopState.from_dict(json.loads(path.read_text())))
        return sorted(states, key=lambda item: item.updated_at, reverse=True)

    def is_locked(self, loop_id: str) -> bool:
        path = lock_file(self.state_root, loop_id)
        if not path.exists():
            return False
        try:
            pid = int(path.read_text().strip())
        except ValueError:
            path.unlink()
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            path.unlink()
            return False
        except PermissionError:
            return True
        return True

    def append_event(self, loop_id: str, event: dict) -> None:
        path = events_file(self.state_root, loop_id)
        ensure_dir(path.parent)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    @contextmanager
    def acquire_lock(self, loop_id: str) -> Iterator[None]:
        path = lock_file(self.state_root, loop_id)
        ensure_dir(path.parent)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(f"Loop is already active: {loop_id}") from exc
        try:
            os.write(fd, str(os.getpid()).encode())
            yield
        finally:
            os.close(fd)
            if path.exists():
                path.unlink()
