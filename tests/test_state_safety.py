import errno
import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ailoop.models import LoopRunConfig
from ailoop.paths import (
    events_file,
    lock_file,
    mutation_lock_file,
    raw_loop_dir,
    retired_file,
    state_file,
    validate_loop_id,
)
from ailoop.service import LoopService
from ailoop.state import (
    StateEventIntegrityError,
    StateLockIntegrityError,
    StateStore,
    _atomic_write,
)


def make_config(prompt: str = "hello") -> LoopRunConfig:
    return LoopRunConfig(
        prompt=prompt,
        runner="echo",
        agent=None,
        steps=1,
        pause_seconds=0,
        continue_on_error=True,
        retry_count=0,
        pre_prompt_enabled=False,
        attach_agent_file=False,
        pre_prompt="",
        agent_file=None,
        runner_command="python3",
        runner_args=["-c", "print('ok')"],
    )


def wait_until(predicate, timeout: float = 5) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


def bind_unix_socket_entry(path: Path) -> socket.socket:
    short_directory = Path(tempfile.mkdtemp(prefix="ailoop-socket-", dir="/tmp"))
    short_path = short_directory / "socket"
    bound_socket = socket.socket(socket.AF_UNIX)
    try:
        bound_socket.bind(str(short_path))
        short_path.rename(path)
        short_directory.rmdir()
    except BaseException:
        bound_socket.close()
        short_path.unlink(missing_ok=True)
        short_directory.rmdir()
        raise
    return bound_socket


LOCK_OPERATIONS = ("mutation", "execution", "inspection")


def state_lock_path(root: Path, loop_id: str, operation: str) -> Path:
    if operation == "mutation":
        return mutation_lock_file(root, loop_id)
    return lock_file(root, loop_id)


def exercise_state_lock(
    store: StateStore,
    loop_id: str,
    operation: str,
    entered: list[bool] | None = None,
) -> None:
    if operation == "inspection":
        store.is_locked(loop_id)
        return
    lock = store.acquire_mutation_lock if operation == "mutation" else store.acquire_lock
    with lock(loop_id):
        if entered is not None:
            entered.append(True)


@pytest.mark.parametrize(
    "loop_id",
    ["../escape", "/absolute", ".hidden", "Upper", "workspaces", "a" * 65, "bad.id"],
)
def test_loop_ids_reject_unsafe_or_reserved_values(loop_id: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Loop id"):
        validate_loop_id(loop_id)
    with pytest.raises(ValueError, match="Loop id"):
        LoopService(tmp_path).create_loop(make_config(), loop_id=loop_id)
    assert list(tmp_path.iterdir()) == []


def test_inspection_of_missing_loop_does_not_create_directory(tmp_path: Path) -> None:
    service = LoopService(tmp_path)

    with pytest.raises(FileNotFoundError):
        service.load_loop("missing-loop")
    with pytest.raises(FileNotFoundError):
        service.loop_paths("missing-loop")

    assert not raw_loop_dir(tmp_path, "missing-loop").exists()


@pytest.mark.parametrize(
    "attack",
    ["symlink", "dangling-symlink", "hardlink", "directory", "fifo", "socket"],
)
def test_event_append_rejects_unsafe_journal_entries(
    tmp_path: Path,
    attack: str,
) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="unsafe-events")
    path = events_file(service.state_root, state.loop_id)
    path.unlink()
    victim = tmp_path / "event-victim"
    victim.write_bytes(b"preserve event victim")
    os.chmod(victim, 0o640)
    victim_mode = stat.S_IMODE(victim.stat().st_mode)
    dangling_target = tmp_path / "missing-event-victim"
    bound_socket: socket.socket | None = None

    if attack == "symlink":
        path.symlink_to(victim)
    elif attack == "dangling-symlink":
        path.symlink_to(dangling_target)
    elif attack == "hardlink":
        os.link(victim, path)
    elif attack == "directory":
        path.mkdir()
    elif attack == "fifo":
        os.mkfifo(path)
    else:
        bound_socket = bind_unix_socket_entry(path)

    try:
        with pytest.raises(StateEventIntegrityError, match="Unsafe event journal"):
            service.store.append_event(state.loop_id, {"event": "unsafe"})
    finally:
        if bound_socket is not None:
            bound_socket.close()

    assert victim.read_bytes() == b"preserve event victim"
    assert stat.S_IMODE(victim.stat().st_mode) == victim_mode
    assert not dangling_target.exists()


@pytest.mark.parametrize(
    "attack",
    ["symlink", "dangling-symlink", "hardlink", "directory", "fifo", "socket"],
)
def test_event_append_rejects_unsafe_state_entries(
    tmp_path: Path,
    attack: str,
) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="unsafe-event-state")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    path = state_file(service.state_root, state.loop_id)
    path.unlink()
    victim = tmp_path / "state-victim"
    victim.write_bytes(b"preserve state victim")
    os.chmod(victim, 0o640)
    victim_mode = stat.S_IMODE(victim.stat().st_mode)
    dangling_target = tmp_path / "missing-state-victim"
    bound_socket: socket.socket | None = None

    if attack == "symlink":
        path.symlink_to(victim)
    elif attack == "dangling-symlink":
        path.symlink_to(dangling_target)
    elif attack == "hardlink":
        os.link(victim, path)
    elif attack == "directory":
        path.mkdir()
    elif attack == "fifo":
        os.mkfifo(path)
    else:
        bound_socket = bind_unix_socket_entry(path)

    try:
        with pytest.raises(StateEventIntegrityError, match="Unsafe event journal state"):
            service.store.append_event(state.loop_id, {"event": "unsafe"})
    finally:
        if bound_socket is not None:
            bound_socket.close()

    assert journal.read_bytes() == original_journal
    assert victim.read_bytes() == b"preserve state victim"
    assert stat.S_IMODE(victim.stat().st_mode) == victim_mode
    assert not dangling_target.exists()


@pytest.mark.parametrize("attack", ["symlink", "file", "group-writable"])
def test_event_append_rejects_unsafe_loop_directories(
    tmp_path: Path,
    attack: str,
) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="unsafe-event-loop")
    directory = raw_loop_dir(service.state_root, state.loop_id)
    original_journal = events_file(service.state_root, state.loop_id).read_bytes()
    preserved = tmp_path / "preserved-loop"

    if attack == "group-writable":
        os.chmod(directory, 0o770)
    else:
        directory.rename(preserved)
        if attack == "symlink":
            directory.symlink_to(preserved, target_is_directory=True)
        else:
            directory.write_text("not a directory")

    with pytest.raises(StateEventIntegrityError, match="Unsafe event journal directory"):
        service.store.append_event(state.loop_id, {"event": "unsafe"})

    if attack == "group-writable":
        journal = directory / "events.jsonl"
    else:
        journal = preserved / "events.jsonl"
    assert journal.read_bytes() == original_journal


@pytest.mark.parametrize(
    "attack",
    [
        "symlink-directory",
        "file-directory",
        "group-writable-directory",
        "symlink-marker",
        "hardlink-marker",
        "group-writable-marker",
    ],
)
def test_event_append_rejects_unsafe_retirement_entries(
    tmp_path: Path,
    attack: str,
) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="unsafe-retirement")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    retirement_root = service.state_root / ".retired"
    external = tmp_path / "external-retired"
    victim = tmp_path / "retirement-victim"
    victim.write_text("preserve retirement victim")

    if attack == "symlink-directory":
        external.mkdir()
        retirement_root.symlink_to(external, target_is_directory=True)
    elif attack == "file-directory":
        retirement_root.write_text("not a directory")
    else:
        retirement_root.mkdir()
        if attack == "group-writable-directory":
            os.chmod(retirement_root, 0o770)
        else:
            marker = retired_file(service.state_root, state.loop_id)
            if attack == "symlink-marker":
                marker.symlink_to(victim)
            elif attack == "hardlink-marker":
                os.link(victim, marker)
            else:
                marker.write_text("{}")
                os.chmod(marker, 0o660)

    with pytest.raises(StateEventIntegrityError, match="Unsafe event journal retirement"):
        service.store.append_event(state.loop_id, {"event": "unsafe"})

    assert journal.read_bytes() == original_journal
    assert victim.read_text() == "preserve retirement victim"


def test_event_append_missing_loop_or_state_returns_false_without_creating_journal(
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path / "state")

    assert service.store.append_event("missing-loop", {"event": "late"}) is False
    assert not raw_loop_dir(service.state_root, "missing-loop").exists()

    state = service.create_loop(make_config(), loop_id="missing-event-state")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    state_file(service.state_root, state.loop_id).unlink()

    assert service.store.append_event(state.loop_id, {"event": "late"}) is False
    assert journal.read_bytes() == original_journal


def test_event_append_validates_loop_id_before_lock_access(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")

    with pytest.raises(ValueError, match="Loop id"):
        service.store.append_event("../unsafe", {"event": "invalid"})

    assert not (service.state_root / ".locks").exists()


def test_event_append_reports_lock_hazard_before_valid_retirement(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="event-lock-precedence")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    retirement = retired_file(service.state_root, state.loop_id)
    retirement.parent.mkdir(mode=0o700)
    retirement.write_text("{}")
    os.chmod(retirement, 0o600)
    mutation_lock = mutation_lock_file(service.state_root, state.loop_id)
    mutation_lock.unlink()
    victim = tmp_path / "event-lock-victim"
    victim.write_text("preserve lock victim")
    mutation_lock.symlink_to(victim)

    with pytest.raises(StateLockIntegrityError, match="Unsafe state lock"):
        service.store.append_event(state.loop_id, {"event": "late"})

    assert journal.read_bytes() == original_journal
    assert victim.read_text() == "preserve lock victim"


def test_event_append_checks_retirement_before_unsafe_loop_directory(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="event-retirement-precedence")
    directory = raw_loop_dir(service.state_root, state.loop_id)
    preserved = tmp_path / "retired-loop-preserved"
    directory.rename(preserved)
    directory.write_text("unsafe loop entry")
    retirement = retired_file(service.state_root, state.loop_id)
    retirement.parent.mkdir(mode=0o700)
    retirement.write_text("{}")
    os.chmod(retirement, 0o600)

    assert service.store.append_event(state.loop_id, {"event": "late"}) is False
    assert (preserved / "events.jsonl").exists()


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_event_append_normalizes_supported_journal_modes(tmp_path: Path, mode: int) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id=f"supported-event-mode-{mode:o}")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    event = {"event": "mode-normalized"}
    payload = (json.dumps(event) + "\n").encode()
    os.chmod(journal, mode)

    assert service.store.append_event(state.loop_id, event) is True
    assert journal.read_bytes() == original_journal + payload
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o400, 0o440, 0o640, 0o660, 0o666, 0o700])
def test_event_append_rejects_unsupported_journal_modes(tmp_path: Path, mode: int) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id=f"unsupported-event-mode-{mode:o}")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    os.chmod(journal, mode)

    with pytest.raises(StateEventIntegrityError, match="Unsafe event journal file"):
        service.store.append_event(state.loop_id, {"event": "unsupported-mode"})

    assert journal.read_bytes() == original_journal
    assert stat.S_IMODE(journal.stat().st_mode) == mode


def test_event_append_rejects_group_writable_state_file(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="unsafe-event-state-mode")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    path = state_file(service.state_root, state.loop_id)
    os.chmod(path, 0o660)

    with pytest.raises(StateEventIntegrityError, match="Unsafe event journal state"):
        service.store.append_event(state.loop_id, {"event": "unsafe-state-mode"})

    assert journal.read_bytes() == original_journal


def test_event_serialization_fails_before_missing_journal_is_created(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="event-serialization")
    journal = events_file(service.state_root, state.loop_id)
    journal.unlink()

    with pytest.raises(TypeError, match="JSON serializable"):
        service.store.append_event(state.loop_id, {"value": object()})

    assert not journal.exists()


def test_event_append_retries_short_writes(monkeypatch, tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="short-event-write")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    event = {"event": "short-write"}
    payload = (json.dumps(event) + "\n").encode()
    original_write = os.write
    journal_descriptor: int | None = None

    def write_short(descriptor: int, data) -> int:  # type: ignore[no-untyped-def]
        nonlocal journal_descriptor
        if journal_descriptor is None and bytes(data) == payload:
            journal_descriptor = descriptor
        if descriptor == journal_descriptor and len(data) > 3:
            return original_write(descriptor, data[:3])
        return original_write(descriptor, data)

    monkeypatch.setattr("ailoop.state.os.write", write_short)

    assert service.store.append_event(state.loop_id, event) is True
    assert journal.read_bytes() == original_journal + payload


def test_event_append_rejects_zero_length_write(monkeypatch, tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="zero-event-write")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    event = {"event": "zero-write"}
    payload = (json.dumps(event) + "\n").encode()
    original_write = os.write

    def write_zero(descriptor: int, data) -> int:  # type: ignore[no-untyped-def]
        if bytes(data) == payload:
            return 0
        return original_write(descriptor, data)

    monkeypatch.setattr("ailoop.state.os.write", write_zero)

    with pytest.raises(OSError, match="Unable to write state event journal") as raised:
        service.store.append_event(state.loop_id, event)

    assert raised.value.errno == errno.EIO
    assert journal.read_bytes() == original_journal


def test_event_append_preserves_partial_write_error(monkeypatch, tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="failed-event-write")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    event = {"event": "partial-error"}
    payload = (json.dumps(event) + "\n").encode()
    original_write = os.write
    journal_descriptor: int | None = None
    wrote_prefix = False

    def fail_after_prefix(descriptor: int, data) -> int:  # type: ignore[no-untyped-def]
        nonlocal journal_descriptor, wrote_prefix
        if journal_descriptor is None and bytes(data) == payload:
            journal_descriptor = descriptor
        if descriptor != journal_descriptor:
            return original_write(descriptor, data)
        if not wrote_prefix:
            wrote_prefix = True
            return original_write(descriptor, data[:5])
        raise OSError(errno.ENOSPC, "injected event write failure")

    monkeypatch.setattr("ailoop.state.os.write", fail_after_prefix)

    with pytest.raises(OSError, match="injected event write failure") as raised:
        service.store.append_event(state.loop_id, event)

    assert raised.value.errno == errno.ENOSPC
    assert journal.read_bytes() == original_journal + payload[:5]


def test_event_append_propagates_file_fsync_failure(monkeypatch, tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="event-file-fsync")
    journal = events_file(service.state_root, state.loop_id)
    journal_identity = journal.stat()
    event = {"event": "file-fsync-failure"}
    payload = (json.dumps(event) + "\n").encode()
    original_fsync = os.fsync

    def fail_event_file_fsync(descriptor: int) -> None:
        observed = os.fstat(descriptor)
        if (observed.st_dev, observed.st_ino) == (journal_identity.st_dev, journal_identity.st_ino):
            raise OSError(errno.EIO, "injected event file fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr("ailoop.state.os.fsync", fail_event_file_fsync)

    with pytest.raises(OSError, match="injected event file fsync failure"):
        service.store.append_event(state.loop_id, event)

    assert journal.read_bytes().endswith(payload)


def test_event_append_ignores_directory_fsync_failure(monkeypatch, tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="event-directory-fsync")
    journal = events_file(service.state_root, state.loop_id)
    event = {"event": "directory-fsync-failure"}
    payload = (json.dumps(event) + "\n").encode()
    original_fsync = os.fsync
    directory_fsync_failures = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal directory_fsync_failures
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsync_failures += 1
            raise OSError(errno.EIO, "injected event directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr("ailoop.state.os.fsync", fail_directory_fsync)

    assert service.store.append_event(state.loop_id, event) is True
    assert directory_fsync_failures == 1
    assert journal.read_bytes().endswith(payload)


@pytest.mark.parametrize("exit_case", ["retired", "missing-loop", "missing-state"])
def test_event_false_result_survives_descriptor_close_failure(
    monkeypatch,
    tmp_path: Path,
    exit_case: str,
) -> None:
    service = LoopService(tmp_path / "state")
    loop_id = f"event-close-{exit_case}"
    if exit_case != "missing-loop":
        state = service.create_loop(make_config(), loop_id=loop_id)
        if exit_case == "retired":
            retirement = retired_file(service.state_root, state.loop_id)
            retirement.parent.mkdir(mode=0o700)
            retirement.write_text("{}")
            os.chmod(retirement, 0o600)
        else:
            state_file(service.state_root, state.loop_id).unlink()
    root_identity = service.state_root.stat()
    original_close = os.close
    leaked_descriptors: set[int] = set()

    def fail_root_close(descriptor: int) -> None:
        observed = os.fstat(descriptor)
        if (observed.st_dev, observed.st_ino) == (root_identity.st_dev, root_identity.st_ino):
            leaked_descriptors.add(descriptor)
            raise OSError(errno.EIO, "injected event root close failure")
        original_close(descriptor)

    try:
        monkeypatch.setattr("ailoop.state.os.close", fail_root_close)
        assert service.store.append_event(loop_id, {"event": "late"}) is False
    finally:
        monkeypatch.undo()
        for descriptor in leaked_descriptors:
            try:
                original_close(descriptor)
            except OSError:
                pass

    assert leaked_descriptors


def test_event_append_rejects_post_open_journal_replacement(monkeypatch, tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="replace-open-event")
    journal = events_file(service.state_root, state.loop_id)
    original_journal = journal.read_bytes()
    displaced = journal.with_suffix(".displaced")
    replacement = b"replacement journal\n"
    original_validate = service.store._validate_open_event_journal
    replaced = False

    def replace_before_validation(opened) -> None:  # type: ignore[no-untyped-def]
        nonlocal replaced
        if not replaced:
            journal.rename(displaced)
            journal.write_bytes(replacement)
            os.chmod(journal, 0o600)
            replaced = True
        original_validate(opened)

    monkeypatch.setattr(service.store, "_validate_open_event_journal", replace_before_validation)

    with pytest.raises(StateEventIntegrityError, match="identity changed"):
        service.store.append_event(state.loop_id, {"event": "unsafe-replacement"})

    assert displaced.read_bytes() == original_journal
    assert journal.read_bytes() == replacement


def test_event_append_tolerates_retirement_directory_disappearing_before_open(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="disappearing-retirement")
    retirement_root = service.state_root / ".retired"
    retirement_root.mkdir(mode=0o700)
    event = {"event": "after-retirement-disappeared"}
    payload = (json.dumps(event) + "\n").encode()
    original_stat = os.stat
    removed = False

    def remove_after_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal removed
        observed = original_stat(path, *args, **kwargs)
        if path == ".retired" and kwargs.get("dir_fd") is not None and not removed:
            retirement_root.rmdir()
            removed = True
        return observed

    monkeypatch.setattr("ailoop.state.os.stat", remove_after_stat)

    assert service.store.append_event(state.loop_id, event) is True
    assert removed is True
    assert events_file(service.state_root, state.loop_id).read_bytes().endswith(payload)


def test_service_keeps_committed_state_when_event_journal_is_unsafe(tmp_path: Path) -> None:
    service = LoopService(tmp_path / "state")
    state = service.create_loop(make_config(), loop_id="unsafe-postcommit-event")
    journal = events_file(service.state_root, state.loop_id)
    journal.unlink()
    victim = tmp_path / "postcommit-victim"
    victim.write_text("preserve postcommit victim")
    journal.symlink_to(victim)

    paused = service.request_control(state.loop_id, "pause")

    assert paused.status == "paused"
    assert paused.control == "pause"
    assert service.load_loop(state.loop_id).to_dict() == paused.to_dict()
    assert victim.read_text() == "preserve postcommit victim"


@pytest.mark.parametrize("operation", LOCK_OPERATIONS)
@pytest.mark.parametrize(
    "attack",
    ["symlink", "dangling-symlink", "hardlink", "directory", "fifo"],
)
def test_state_locks_reject_unsafe_final_entries(
    tmp_path: Path,
    operation: str,
    attack: str,
) -> None:
    root = tmp_path / "state"
    store = StateStore(root)
    loop_id = f"unsafe-{operation}"
    path = state_lock_path(root, loop_id, operation)
    path.parent.mkdir(mode=0o700)
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve me")
    os.chmod(victim, 0o640)
    victim_mode = stat.S_IMODE(victim.stat().st_mode)
    dangling_target = tmp_path / "missing-victim"

    if attack == "symlink":
        path.symlink_to(victim)
    elif attack == "dangling-symlink":
        path.symlink_to(dangling_target)
    elif attack == "hardlink":
        os.link(victim, path)
    elif attack == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)

    entered: list[bool] = []
    with pytest.raises(StateLockIntegrityError, match="Unsafe state lock"):
        exercise_state_lock(store, loop_id, operation, entered)

    assert entered == []
    assert victim.read_bytes() == b"preserve me"
    assert stat.S_IMODE(victim.stat().st_mode) == victim_mode
    assert not dangling_target.exists()


@pytest.mark.parametrize("operation", LOCK_OPERATIONS)
def test_state_locks_reject_symlinked_lock_directory(
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    external = tmp_path / "external-locks"
    external.mkdir()
    (root / ".locks").symlink_to(external, target_is_directory=True)
    store = StateStore(root)

    with pytest.raises(StateLockIntegrityError, match="Unsafe state lock directory"):
        exercise_state_lock(store, "unsafe-directory", operation)

    assert list(external.iterdir()) == []


@pytest.mark.parametrize("unsafe_target", ["state-root", "lock-directory"])
def test_state_locks_reject_group_writable_namespaces(
    tmp_path: Path,
    unsafe_target: str,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    if unsafe_target == "state-root":
        os.chmod(root, 0o770)
    else:
        locks = root / ".locks"
        locks.mkdir(mode=0o700)
        os.chmod(locks, 0o770)
    store = StateStore(root)

    with pytest.raises(StateLockIntegrityError, match="Unsafe state lock directory"):
        with store.acquire_lock("unsafe-namespace"):
            pytest.fail("unsafe namespace must not be entered")


@pytest.mark.parametrize("operation", LOCK_OPERATIONS)
def test_state_locks_fail_closed_without_no_follow(
    monkeypatch,
    tmp_path: Path,
    operation: str,
) -> None:
    store = StateStore(tmp_path / "state")
    monkeypatch.setattr("ailoop.state.os.O_NOFOLLOW", 0)

    with pytest.raises(StateLockIntegrityError, match="no-follow"):
        exercise_state_lock(store, "missing-no-follow", operation)


@pytest.mark.parametrize("operation", LOCK_OPERATIONS)
def test_state_locks_reject_path_replacement_after_flock(
    monkeypatch,
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "state"
    store = StateStore(root)
    loop_id = f"replace-{operation}"
    path = state_lock_path(root, loop_id, operation)
    path.parent.mkdir(mode=0o700)
    path.write_bytes(b"original diagnostic")
    os.chmod(path, 0o600)
    displaced = path.with_suffix(".displaced")
    original_flock = fcntl.flock
    replaced = False

    def replace_after_flock(descriptor: int, operation_code: int) -> None:
        nonlocal replaced
        original_flock(descriptor, operation_code)
        if not replaced and operation_code & fcntl.LOCK_EX:
            path.rename(displaced)
            path.write_bytes(b"replacement diagnostic")
            replaced = True

    monkeypatch.setattr("ailoop.state.fcntl.flock", replace_after_flock)

    with pytest.raises(StateLockIntegrityError, match="identity changed"):
        exercise_state_lock(store, loop_id, operation)

    assert displaced.read_bytes() == b"original diagnostic"
    assert path.read_bytes() == b"replacement diagnostic"


@pytest.mark.parametrize("operation", ["execution", "inspection"])
def test_busy_state_locks_validate_identity_before_reporting_busy(
    monkeypatch,
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "state"
    store = StateStore(root)
    loop_id = f"busy-replace-{operation}"
    path = lock_file(root, loop_id)
    path.parent.mkdir(mode=0o700)
    path.write_bytes(b"held diagnostic")
    os.chmod(path, 0o600)
    holder = os.open(path, os.O_RDWR)
    original_flock = fcntl.flock
    original_flock(holder, fcntl.LOCK_EX)
    displaced = path.with_suffix(".displaced")
    replaced = False

    def replace_when_busy(descriptor: int, operation_code: int) -> None:
        nonlocal replaced
        try:
            original_flock(descriptor, operation_code)
        except BlockingIOError:
            if not replaced:
                path.rename(displaced)
                path.write_bytes(b"replacement diagnostic")
                replaced = True
            raise

    monkeypatch.setattr("ailoop.state.fcntl.flock", replace_when_busy)
    try:
        with pytest.raises(StateLockIntegrityError, match="identity changed"):
            exercise_state_lock(store, loop_id, operation)
    finally:
        original_flock(holder, fcntl.LOCK_UN)
        os.close(holder)

    assert displaced.read_bytes() == b"held diagnostic"
    assert path.read_bytes() == b"replacement diagnostic"


def test_unlocked_inspection_preserves_diagnostics_and_private_modes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    store = StateStore(root)
    path = lock_file(root, "diagnostic-lock")
    path.parent.mkdir(mode=0o755)
    path.write_bytes(b"stale pid and notes")
    os.chmod(path, 0o644)

    assert store.is_locked("diagnostic-lock") is False

    assert path.read_bytes() == b"stale pid and notes"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_execution_lock_completes_partial_pid_writes(monkeypatch, tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    path = lock_file(store.state_root, "partial-pid")
    original_write = os.write
    write_attempts = 0

    def write_one_byte(descriptor: int, payload: bytes | memoryview) -> int:
        nonlocal write_attempts
        write_attempts += 1
        return original_write(descriptor, payload[:1])

    monkeypatch.setattr("ailoop.state.os.write", write_one_byte)

    with store.acquire_lock("partial-pid"):
        assert path.read_text() == str(os.getpid())

    assert write_attempts == len(str(os.getpid()))


def test_zero_length_pid_write_releases_execution_lock(monkeypatch, tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    entered = False

    with monkeypatch.context() as scoped:
        scoped.setattr("ailoop.state.os.write", lambda _descriptor, _payload: 0)
        with pytest.raises(OSError, match="Unable to write state lock PID"):
            with store.acquire_lock("zero-pid"):
                entered = True

    assert entered is False
    with store.acquire_lock("zero-pid"):
        pass


def test_execution_flock_is_cross_process_and_permanent(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    code = """
import sys
import time
from pathlib import Path
from ailoop.state import StateStore

root, ready, release = map(Path, sys.argv[1:])
with StateStore(root).acquire_lock("shared-loop"):
    ready.write_text("ready")
    while not release.exists():
        time.sleep(0.02)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(ready), str(release)]
    )
    store = StateStore(tmp_path)
    try:
        wait_until(lambda: ready.exists() or process.poll() is not None)
        assert process.poll() is None
        assert store.is_locked("shared-loop") is True
        with pytest.raises(RuntimeError, match="already active"):
            with store.acquire_lock("shared-loop"):
                pass
    finally:
        release.write_text("release")
        process.wait(timeout=5)

    assert store.is_locked("shared-loop") is False
    assert lock_file(tmp_path, "shared-loop").exists()


@pytest.mark.parametrize("lock_method", ["acquire_mutation_lock", "acquire_lock"])
def test_unlock_failure_preserves_primary_result_and_releases_lock(
    monkeypatch,
    tmp_path: Path,
    lock_method: str,
) -> None:
    store = StateStore(tmp_path)
    original_flock = fcntl.flock
    unlock_attempts = 0

    def fail_unlock(descriptor: int, operation: int) -> None:
        nonlocal unlock_attempts
        if operation == fcntl.LOCK_UN:
            unlock_attempts += 1
            raise OSError("unlock failed")
        original_flock(descriptor, operation)

    monkeypatch.setattr("ailoop.state.fcntl.flock", fail_unlock)
    lock = getattr(store, lock_method)

    with pytest.raises(RuntimeError, match="primary body failure"):
        with lock("cleanup-lock"):
            raise RuntimeError("primary body failure")

    with lock("cleanup-lock"):
        pass

    assert unlock_attempts == 2


def test_execution_lock_pre_yield_fsync_failure_releases_lock(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            "ailoop.state.os.fsync",
            lambda _descriptor: (_ for _ in ()).throw(OSError("pid fsync failed")),
        )
        with pytest.raises(OSError, match="pid fsync failed"):
            with store.acquire_lock("pre-yield-lock"):
                pytest.fail("lock body must not run")

    with store.acquire_lock("pre-yield-lock"):
        pass


def test_concurrent_create_if_absent_preserves_one_winner(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)

    def create(prompt: str) -> str:
        barrier.wait(timeout=5)
        service = LoopService(tmp_path)
        service.create_loop(make_config(prompt), loop_id="same-id")
        return prompt

    outcomes: list[str] = []
    failures: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create, prompt) for prompt in ("first", "second")]
        for future in futures:
            try:
                outcomes.append(future.result(timeout=5))
            except BaseException as exc:
                failures.append(exc)

    assert len(outcomes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert LoopService(tmp_path).load_loop("same-id").run_config.prompt == outcomes[0]


def test_concurrent_mutations_keep_json_valid_and_updates_lossless(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    service.create_loop(make_config(), loop_id="mutations")
    stop_reading = threading.Event()
    read_errors: list[BaseException] = []

    def read_continuously() -> None:
        while not stop_reading.is_set():
            try:
                payload = json.loads((tmp_path / "mutations" / "state.json").read_text())
                assert payload["loop_id"] == "mutations"
            except BaseException as exc:
                read_errors.append(exc)
                stop_reading.set()

    def increment() -> None:
        for _ in range(40):
            with service.store.mutate("mutations") as state:
                state.consecutive_failures += 1

    reader = threading.Thread(target=read_continuously)
    reader.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(increment) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)
    finally:
        stop_reading.set()
        reader.join(timeout=5)

    assert read_errors == []
    assert service.load_loop("mutations").consecutive_failures == 80


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_atomic_write_failure_preserves_old_file_and_cleans_temp(
    monkeypatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    path = tmp_path / "state.json"
    path.write_text("old")

    def fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(f"injected {failure_point} failure")

    monkeypatch.setattr(f"ailoop.state.os.{failure_point}", fail)
    with pytest.raises(OSError, match="injected"):
        _atomic_write(path, "new")

    assert path.read_text() == "old"
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_retired_loop_id_cannot_be_recreated_or_mutated(tmp_path: Path) -> None:
    service = LoopService(tmp_path)
    service.create_loop(make_config(), loop_id="retired-loop")
    service.remove_loop("retired-loop", force=True)

    assert not raw_loop_dir(tmp_path, "retired-loop").exists()
    assert service.store.append_event("retired-loop", {"event": "late"}) is False
    with pytest.raises(RuntimeError, match="retired"):
        service.create_loop(make_config("replacement"), loop_id="retired-loop")
    with pytest.raises(RuntimeError, match="retired"):
        with service.store.mutate("retired-loop"):
            pass


def test_retirement_reservation_fails_closed_before_rename(
    monkeypatch,
    tmp_path: Path,
) -> None:
    service = LoopService(tmp_path)
    service.create_loop(make_config(), loop_id="retire-crash")
    directory = raw_loop_dir(tmp_path, "retire-crash")
    original_rename = Path.rename

    def fail_rename(path: Path, target: Path) -> Path:
        if path == directory:
            raise OSError("injected rename crash")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(OSError, match="rename crash"):
        service.remove_loop("retire-crash", force=True)

    assert directory.exists()
    assert service.store.is_retired("retire-crash") is True
    assert service.store.append_event("retire-crash", {"event": "late"}) is False
    with pytest.raises(RuntimeError, match="retired"):
        service.run_loop("retire-crash")
    with pytest.raises(RuntimeError, match="retired"):
        service.create_loop(make_config(), loop_id="retire-crash")

    monkeypatch.setattr(Path, "rename", original_rename)
    service.remove_loop("retire-crash", force=True)
    assert not directory.exists()
