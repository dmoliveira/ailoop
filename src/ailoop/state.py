from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
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


class StateLockIntegrityError(OSError):
    """Raised when a state lock path cannot be trusted safely."""


class StateEventIntegrityError(OSError):
    """Raised when an event journal path cannot be trusted safely."""


@dataclass(slots=True)
class _OpenedStateLock:
    path: Path
    name: str
    root_descriptor: int
    directory_descriptor: int
    descriptor: int


@dataclass(slots=True)
class _OpenedEventJournal:
    path: Path
    loop_id: str
    root_descriptor: int
    loop_descriptor: int
    descriptor: int


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
        self.state_root = ensure_dir(Path(os.path.abspath(state_root)))

    @staticmethod
    def _effective_uid() -> int | None:
        return os.geteuid() if hasattr(os, "geteuid") else None

    @classmethod
    def _validate_lock_directory_identity(
        cls,
        descriptor: int,
        named: os.stat_result,
        path: Path,
    ) -> None:
        opened = os.fstat(descriptor)
        expected_uid = cls._effective_uid()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or (expected_uid is not None and opened.st_uid != expected_uid)
            or (expected_uid is not None and named.st_uid != expected_uid)
            or opened.st_mode & 0o022
            or named.st_mode & 0o022
        ):
            raise StateLockIntegrityError(f"Unsafe state lock directory: {path}")

    def _validate_state_root(self, descriptor: int) -> None:
        try:
            named = os.stat(self.state_root, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise StateLockIntegrityError(
                f"Unsafe state lock directory identity changed: {self.state_root}"
            ) from exc
        self._validate_lock_directory_identity(descriptor, named, self.state_root)

    def _validate_lock_directory(
        self,
        root_descriptor: int,
        directory_descriptor: int,
    ) -> None:
        path = self.state_root / ".locks"
        try:
            named = os.stat(".locks", dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise StateLockIntegrityError(
                f"Unsafe state lock directory identity changed: {path}"
            ) from exc
        self._validate_lock_directory_identity(directory_descriptor, named, path)

    @classmethod
    def _validate_lock_file_identity(cls, opened: _OpenedStateLock) -> None:
        try:
            descriptor_state = os.fstat(opened.descriptor)
            named_state = os.stat(
                opened.name,
                dir_fd=opened.directory_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise StateLockIntegrityError(
                f"Unsafe state lock identity changed: {opened.path}"
            ) from exc
        expected_uid = cls._effective_uid()
        if (descriptor_state.st_dev, descriptor_state.st_ino) != (
            named_state.st_dev,
            named_state.st_ino,
        ):
            raise StateLockIntegrityError(f"Unsafe state lock identity changed: {opened.path}")
        if (
            not stat.S_ISREG(descriptor_state.st_mode)
            or not stat.S_ISREG(named_state.st_mode)
            or descriptor_state.st_nlink != 1
            or named_state.st_nlink != 1
            or (expected_uid is not None and descriptor_state.st_uid != expected_uid)
            or (expected_uid is not None and named_state.st_uid != expected_uid)
        ):
            raise StateLockIntegrityError(f"Unsafe state lock file: {opened.path}")

    @classmethod
    def _preflight_lock_file(
        cls,
        directory_descriptor: int,
        name: str,
        path: Path,
    ) -> None:
        try:
            observed = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        expected_uid = cls._effective_uid()
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (expected_uid is not None and observed.st_uid != expected_uid)
        ):
            raise StateLockIntegrityError(f"Unsafe state lock file: {path}")

    def _validate_open_lock(self, opened: _OpenedStateLock) -> None:
        self._validate_state_root(opened.root_descriptor)
        self._validate_lock_directory(
            opened.root_descriptor,
            opened.directory_descriptor,
        )
        self._validate_lock_file_identity(opened)

    @staticmethod
    def _close_open_lock(opened: _OpenedStateLock) -> None:
        for descriptor in (
            opened.descriptor,
            opened.directory_descriptor,
            opened.root_descriptor,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass

    @staticmethod
    def _normalize_unsafe_open(
        path: Path,
        error: OSError,
        *,
        kind: str = "path",
    ) -> None:
        if error.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO}:
            raise StateLockIntegrityError(f"Unsafe state lock {kind}: {path}") from error
        raise error

    def _open_lock_file(self, path: Path) -> _OpenedStateLock:
        # Directory descriptors pin the validated namespace. The configured
        # state-root ancestry and processes sharing our effective UID remain trusted.
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow:
            raise StateLockIntegrityError("Secure no-follow state locking is unavailable")
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if not directory_flag:
            raise StateLockIntegrityError("Secure state lock directory opening is unavailable")
        directory_flags = os.O_RDONLY | directory_flag | no_follow | getattr(os, "O_CLOEXEC", 0)
        root_descriptor = -1
        directory_descriptor = -1
        descriptor = -1
        try:
            try:
                root_descriptor = os.open(self.state_root, directory_flags)
            except OSError as exc:
                self._normalize_unsafe_open(self.state_root, exc, kind="directory")
            self._validate_state_root(root_descriptor)
            try:
                os.mkdir(".locks", mode=0o700, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            try:
                directory_descriptor = os.open(
                    ".locks",
                    directory_flags,
                    dir_fd=root_descriptor,
                )
            except OSError as exc:
                self._normalize_unsafe_open(
                    self.state_root / ".locks",
                    exc,
                    kind="directory",
                )
            self._validate_lock_directory(root_descriptor, directory_descriptor)
            os.fchmod(directory_descriptor, 0o700)

            self._preflight_lock_file(directory_descriptor, path.name, path)
            file_flags = (
                os.O_RDWR | no_follow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
            )
            for _attempt in range(3):
                try:
                    descriptor = os.open(
                        path.name,
                        file_flags,
                        dir_fd=directory_descriptor,
                    )
                except FileNotFoundError:
                    try:
                        descriptor = os.open(
                            path.name,
                            file_flags | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=directory_descriptor,
                        )
                    except FileExistsError:
                        continue
                    except OSError as exc:
                        self._normalize_unsafe_open(path, exc)
                except OSError as exc:
                    self._normalize_unsafe_open(path, exc)
                break
            else:
                raise StateLockIntegrityError(
                    f"Unsafe state lock identity changed during creation: {path}"
                )
            opened = _OpenedStateLock(
                path=path,
                name=path.name,
                root_descriptor=root_descriptor,
                directory_descriptor=directory_descriptor,
                descriptor=descriptor,
            )
            self._validate_open_lock(opened)
            os.fchmod(descriptor, 0o600)
            return opened
        except BaseException:
            for opened_descriptor in (descriptor, directory_descriptor, root_descriptor):
                if opened_descriptor >= 0:
                    try:
                        os.close(opened_descriptor)
                    except OSError:
                        pass
            raise

    @staticmethod
    def _write_all(descriptor: int, payload: bytes, error_message: str) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError(errno.EIO, error_message)
            remaining = remaining[written:]

    @classmethod
    def _validate_event_directory_identity(
        cls,
        descriptor: int,
        named: os.stat_result,
        path: Path,
        *,
        kind: str = "directory",
    ) -> None:
        opened = os.fstat(descriptor)
        expected_uid = cls._effective_uid()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or (expected_uid is not None and opened.st_uid != expected_uid)
            or (expected_uid is not None and named.st_uid != expected_uid)
            or opened.st_mode & 0o022
            or named.st_mode & 0o022
        ):
            raise StateEventIntegrityError(f"Unsafe event journal {kind}: {path}")

    @classmethod
    def _validate_event_regular_entry(
        cls,
        observed: os.stat_result,
        path: Path,
        *,
        kind: str,
        allowed_modes: set[int] | None = None,
    ) -> None:
        expected_uid = cls._effective_uid()
        mode = stat.S_IMODE(observed.st_mode)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (expected_uid is not None and observed.st_uid != expected_uid)
            or mode & 0o022
            or (allowed_modes is not None and mode not in allowed_modes)
        ):
            raise StateEventIntegrityError(f"Unsafe event journal {kind}: {path}")

    @staticmethod
    def _normalize_unsafe_event_open(
        path: Path,
        error: OSError,
        *,
        kind: str,
    ) -> None:
        if error.errno in {errno.ELOOP, errno.EISDIR, errno.ENOTDIR, errno.ENXIO}:
            raise StateEventIntegrityError(f"Unsafe event journal {kind}: {path}") from error
        raise error

    def _validate_event_root(self, descriptor: int) -> None:
        try:
            named = os.stat(self.state_root, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise StateEventIntegrityError(
                f"Unsafe event journal directory identity changed: {self.state_root}"
            ) from exc
        self._validate_event_directory_identity(descriptor, named, self.state_root)

    def _validate_event_loop_directory(
        self,
        root_descriptor: int,
        loop_descriptor: int,
        loop_id: str,
    ) -> None:
        path = raw_loop_dir(self.state_root, loop_id)
        try:
            named = os.stat(loop_id, dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise StateEventIntegrityError(
                f"Unsafe event journal directory identity changed: {path}"
            ) from exc
        self._validate_event_directory_identity(loop_descriptor, named, path)

    def _secure_event_is_retired(
        self,
        root_descriptor: int,
        loop_id: str,
        directory_flags: int,
    ) -> bool:
        directory_path = self.state_root / ".retired"
        try:
            os.stat(".retired", dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        directory_descriptor = -1
        try:
            try:
                directory_descriptor = os.open(
                    ".retired",
                    directory_flags,
                    dir_fd=root_descriptor,
                )
            except FileNotFoundError:
                return False
            except OSError as exc:
                self._normalize_unsafe_event_open(
                    directory_path,
                    exc,
                    kind="retirement directory",
                )
            named_directory = os.stat(
                ".retired",
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            self._validate_event_directory_identity(
                directory_descriptor,
                named_directory,
                directory_path,
                kind="retirement directory",
            )
            marker_path = retired_file(self.state_root, loop_id)
            try:
                marker = os.stat(
                    marker_path.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            self._validate_event_regular_entry(
                marker,
                marker_path,
                kind="retirement marker",
            )
            return True
        finally:
            if directory_descriptor >= 0:
                try:
                    os.close(directory_descriptor)
                except OSError:
                    pass

    def _secure_event_state_exists(self, loop_descriptor: int, loop_id: str) -> bool:
        path = state_file(self.state_root, loop_id)
        try:
            observed = os.stat("state.json", dir_fd=loop_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        self._validate_event_regular_entry(observed, path, kind="state")
        return True

    def _preflight_event_file(self, loop_descriptor: int, path: Path) -> bool:
        try:
            observed = os.stat(path.name, dir_fd=loop_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        self._validate_event_regular_entry(
            observed,
            path,
            kind="file",
            allowed_modes={0o600, 0o644},
        )
        return True

    def _validate_event_file_identity(
        self,
        opened: _OpenedEventJournal,
        *,
        allowed_modes: set[int] | None,
    ) -> None:
        try:
            descriptor_state = os.fstat(opened.descriptor)
            named_state = os.stat(
                opened.path.name,
                dir_fd=opened.loop_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            raise StateEventIntegrityError(
                f"Unsafe event journal identity changed: {opened.path}"
            ) from exc
        if (descriptor_state.st_dev, descriptor_state.st_ino) != (
            named_state.st_dev,
            named_state.st_ino,
        ):
            raise StateEventIntegrityError(f"Unsafe event journal identity changed: {opened.path}")
        self._validate_event_regular_entry(
            descriptor_state,
            opened.path,
            kind="file",
            allowed_modes=allowed_modes,
        )
        self._validate_event_regular_entry(
            named_state,
            opened.path,
            kind="file",
            allowed_modes=allowed_modes,
        )

    def _validate_open_event_journal(self, opened: _OpenedEventJournal) -> None:
        self._validate_event_root(opened.root_descriptor)
        self._validate_event_loop_directory(
            opened.root_descriptor,
            opened.loop_descriptor,
            opened.loop_id,
        )
        if not self._secure_event_state_exists(opened.loop_descriptor, opened.loop_id):
            raise StateEventIntegrityError(
                f"Unsafe event journal state identity changed: "
                f"{state_file(self.state_root, opened.loop_id)}"
            )
        self._validate_event_file_identity(opened, allowed_modes={0o600})

    @staticmethod
    def _close_event_descriptors(*descriptors: int) -> None:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    @classmethod
    def _close_open_event_journal(cls, opened: _OpenedEventJournal) -> None:
        cls._close_event_descriptors(
            opened.descriptor,
            opened.loop_descriptor,
            opened.root_descriptor,
        )

    def _open_event_journal(
        self,
        loop_id: str,
        event: dict,
    ) -> tuple[_OpenedEventJournal, bytes] | None:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        non_block = getattr(os, "O_NONBLOCK", 0)
        if not no_follow:
            raise StateEventIntegrityError("Secure no-follow event journaling is unavailable")
        if not directory_flag:
            raise StateEventIntegrityError("Secure event journal directory opening is unavailable")
        if not non_block:
            raise StateEventIntegrityError("Secure nonblocking event journaling is unavailable")
        directory_flags = os.O_RDONLY | directory_flag | no_follow | getattr(os, "O_CLOEXEC", 0)
        root_descriptor = -1
        loop_descriptor = -1
        descriptor = -1
        try:
            try:
                root_descriptor = os.open(self.state_root, directory_flags)
            except OSError as exc:
                self._normalize_unsafe_event_open(
                    self.state_root,
                    exc,
                    kind="directory",
                )
            self._validate_event_root(root_descriptor)
            if self._secure_event_is_retired(root_descriptor, loop_id, directory_flags):
                self._close_event_descriptors(root_descriptor)
                return None
            try:
                loop_descriptor = os.open(
                    loop_id,
                    directory_flags,
                    dir_fd=root_descriptor,
                )
            except FileNotFoundError:
                self._close_event_descriptors(root_descriptor)
                return None
            except OSError as exc:
                self._normalize_unsafe_event_open(
                    raw_loop_dir(self.state_root, loop_id),
                    exc,
                    kind="directory",
                )
            self._validate_event_loop_directory(root_descriptor, loop_descriptor, loop_id)
            if not self._secure_event_state_exists(loop_descriptor, loop_id):
                self._close_event_descriptors(loop_descriptor, root_descriptor)
                return None

            path = events_file(self.state_root, loop_id)
            existed = self._preflight_event_file(loop_descriptor, path)
            payload = (json.dumps(event) + "\n").encode()
            file_flags = (
                os.O_WRONLY | os.O_APPEND | no_follow | non_block | getattr(os, "O_CLOEXEC", 0)
            )
            created = False
            for _attempt in range(3):
                try:
                    descriptor = os.open(
                        path.name,
                        file_flags,
                        dir_fd=loop_descriptor,
                    )
                except FileNotFoundError:
                    try:
                        descriptor = os.open(
                            path.name,
                            file_flags | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=loop_descriptor,
                        )
                        created = True
                    except FileExistsError:
                        continue
                    except OSError as exc:
                        self._normalize_unsafe_event_open(path, exc, kind="file")
                except OSError as exc:
                    self._normalize_unsafe_event_open(path, exc, kind="file")
                break
            else:
                raise StateEventIntegrityError(
                    f"Unsafe event journal identity changed during creation: {path}"
                )
            opened = _OpenedEventJournal(
                path=path,
                loop_id=loop_id,
                root_descriptor=root_descriptor,
                loop_descriptor=loop_descriptor,
                descriptor=descriptor,
            )
            self._validate_event_file_identity(
                opened,
                allowed_modes=None if created else {0o600, 0o644},
            )
            if existed and created:
                raise StateEventIntegrityError(
                    f"Unsafe event journal identity changed during creation: {path}"
                )
            os.fchmod(descriptor, 0o600)
            self._validate_open_event_journal(opened)
            return opened, payload
        except BaseException:
            for opened_descriptor in (descriptor, loop_descriptor, root_descriptor):
                if opened_descriptor >= 0:
                    try:
                        os.close(opened_descriptor)
                    except OSError:
                        pass
            raise

    @classmethod
    def _write_lock_pid(cls, descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        cls._write_all(descriptor, str(os.getpid()).encode(), "Unable to write state lock PID")
        os.fsync(descriptor)

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
                directory.mkdir(mode=0o700)
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
        opened = self._open_lock_file(path)
        acquired = False
        try:
            fcntl.flock(opened.descriptor, fcntl.LOCK_EX)
            acquired = True
            self._validate_open_lock(opened)
            yield
        finally:
            # The protected operation may already have committed. Preserve its
            # result or primary exception while still attempting both cleanup steps.
            if acquired:
                try:
                    fcntl.flock(opened.descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            self._close_open_lock(opened)

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
        opened = self._open_lock_file(path)
        acquired = False
        result: bool
        try:
            try:
                fcntl.flock(opened.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._validate_open_lock(opened)
                result = True
            else:
                acquired = True
                self._validate_open_lock(opened)
                result = False
        except BaseException:
            if acquired:
                try:
                    fcntl.flock(opened.descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            raise
        else:
            if acquired:
                fcntl.flock(opened.descriptor, fcntl.LOCK_UN)
            return result
        finally:
            self._close_open_lock(opened)

    def append_event(self, loop_id: str, event: dict) -> bool:
        validate_loop_id(loop_id)
        with self.acquire_mutation_lock(loop_id):
            prepared = self._open_event_journal(loop_id, event)
            if prepared is None:
                return False
            opened, payload = prepared
            try:
                self._validate_open_event_journal(opened)
                self._write_all(
                    opened.descriptor,
                    payload,
                    "Unable to write state event journal",
                )
                os.fsync(opened.descriptor)
                try:
                    os.fsync(opened.loop_descriptor)
                except OSError:
                    pass
                return True
            finally:
                self._close_open_event_journal(opened)

    @contextmanager
    def acquire_lock(self, loop_id: str) -> Iterator[None]:
        path = lock_file(self.state_root, loop_id)
        opened = self._open_lock_file(path)
        acquired = False
        try:
            try:
                fcntl.flock(opened.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._validate_open_lock(opened)
                raise RuntimeError(f"Loop is already active: {loop_id}") from exc
            acquired = True
            self._validate_open_lock(opened)
            self._write_lock_pid(opened.descriptor)
            yield
        finally:
            if acquired:
                try:
                    fcntl.flock(opened.descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            self._close_open_lock(opened)
