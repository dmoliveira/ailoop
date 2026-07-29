import fcntl
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ailoop.models import LoopRunConfig
from ailoop.paths import lock_file, raw_loop_dir, validate_loop_id
from ailoop.service import LoopService
from ailoop.state import StateStore, _atomic_write


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
