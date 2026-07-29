from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from ailoop.memory import (
    MAX_MEMORY_ID_ATTEMPTS,
    MemoryIntegrityError,
    MemoryStorageError,
    MemoryStore,
)
from ailoop.models import LoopRunConfig


def make_run_config() -> LoopRunConfig:
    return LoopRunConfig(
        prompt="remember",
        runner="test",
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
        runner_args=[],
    )


def create_memory_in_process(
    state_root: str,
    folder: str,
    kind: str,
    title: str,
    collision_id: str,
    replacement_id: str,
    ready,
    start,
    results,
) -> None:
    store = MemoryStore(Path(state_root))
    ready.put(title)
    if not start.wait(timeout=10):
        results.put(("error", title, "start timeout"))
        return
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        side_effect=[
            SimpleNamespace(hex=collision_id),
            SimpleNamespace(hex=replacement_id),
        ],
    ):
        try:
            entry = store.create(
                kind=kind,  # type: ignore[arg-type]
                title=title,
                run_config=make_run_config(),
                folder=Path(folder),
            )
        except Exception as exc:  # pragma: no cover - reported to the parent process
            results.put(("error", title, repr(exc)))
        else:
            results.put(("ok", entry.id, entry.title))


@pytest.mark.parametrize(
    ("existing_kind", "new_kind"),
    [("preset", "preset"), ("preset", "history")],
)
def test_memory_create_retries_collisions_without_overwrite(
    existing_kind: str,
    new_kind: str,
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path)
    collision_id = "a" * 12
    replacement_id = "b" * 12
    values = [collision_id, collision_id, replacement_id]
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        side_effect=[SimpleNamespace(hex=value) for value in values],
    ):
        existing = store.create(
            kind=existing_kind,  # type: ignore[arg-type]
            title="existing",
            run_config=make_run_config(),
            folder=tmp_path,
        )
        existing_path = store._entry_path(existing.kind, existing.id)
        existing_bytes = existing_path.read_bytes()
        created = store.create(
            kind=new_kind,  # type: ignore[arg-type]
            title="new",
            run_config=make_run_config(),
            folder=tmp_path,
        )

    assert existing.id == collision_id
    assert created.id == replacement_id
    assert existing_path.read_bytes() == existing_bytes
    assert store.load(collision_id, folder=tmp_path).title == "existing"
    assert store.load(replacement_id, folder=tmp_path).title == "new"


def test_memory_create_exhaustion_fails_closed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    collision_id = "c" * 12
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=collision_id),
    ) as generate:
        existing = store.create(
            kind="preset",
            title="existing",
            run_config=make_run_config(),
            folder=tmp_path,
        )
        path = store._entry_path(existing.kind, existing.id)
        original = path.read_bytes()
        with pytest.raises(RuntimeError, match=f"after {MAX_MEMORY_ID_ATTEMPTS} attempts"):
            store.create(
                kind="history",
                title="never created",
                run_config=make_run_config(),
                folder=tmp_path,
            )

    assert generate.call_count == MAX_MEMORY_ID_ATTEMPTS + 1
    assert path.read_bytes() == original
    assert list(store.history_dir.glob("*.json")) == []


def test_public_save_upserts_same_kind_and_rejects_cross_kind_collision(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry = store.create(
        kind="preset",
        title="existing",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    path = store._entry_path(entry.kind, entry.id)
    updated = deepcopy(entry)
    updated.title = "updated"
    store.save(updated)
    updated_bytes = path.read_bytes()

    assert store.load(entry.id, folder=tmp_path).title == "updated"
    duplicate = deepcopy(entry)
    duplicate.kind = "history"
    with pytest.raises(FileExistsError, match="already exists"):
        store.save(duplicate)

    assert path.read_bytes() == updated_bytes
    assert not store._entry_path("history", entry.id).exists()


def test_public_save_rejects_unmanaged_hardlink_without_touching_target(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry = store.create(
        kind="preset",
        title="existing",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    path = store._entry_path(entry.kind, entry.id)
    outside = tmp_path / "outside-entry.json"
    path.replace(outside)
    os.link(outside, path)
    original = outside.read_bytes()

    updated = deepcopy(entry)
    updated.title = "must not write"
    with pytest.raises(MemoryIntegrityError, match="private regular file"):
        store.save(updated)

    assert outside.read_bytes() == original
    assert path.read_bytes() == original


def test_broken_symlink_is_occupied_without_touching_target(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    collision_id = "d" * 12
    replacement_id = "e" * 12
    broken_target = tmp_path / "missing-target"
    occupied = store._entry_path("preset", collision_id)
    occupied.symlink_to(broken_target)
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        side_effect=[SimpleNamespace(hex=collision_id), SimpleNamespace(hex=replacement_id)],
    ):
        created = store.create(
            kind="history",
            title="safe",
            run_config=make_run_config(),
            folder=tmp_path,
        )

    assert created.id == replacement_id
    assert occupied.is_symlink()
    assert os.readlink(occupied) == str(broken_target)


def test_lock_symlink_fails_closed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "f" * 12
    outside = tmp_path / "outside-lock-target"
    outside.write_text("unchanged", encoding="utf-8")
    (store.locks_dir / f"{entry_id}.lock").symlink_to(outside)

    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=entry_id),
    ):
        with pytest.raises(OSError):
            store.create(
                kind="preset",
                title="blocked",
                run_config=make_run_config(),
                folder=tmp_path,
            )

    assert outside.read_text(encoding="utf-8") == "unchanged"
    assert list(store.presets_dir.glob("*.json")) == []


def test_nonregular_lock_path_fails_closed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "0" * 12
    (store.locks_dir / f"{entry_id}.lock").mkdir()
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=entry_id),
    ):
        with pytest.raises(OSError):
            store.create(
                kind="preset",
                title="blocked",
                run_config=make_run_config(),
                folder=tmp_path,
            )

    assert list(store.presets_dir.glob("*.json")) == []


def test_missing_no_follow_support_fails_closed(tmp_path: Path, monkeypatch) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "3" * 12
    monkeypatch.delattr("ailoop.memory.os.O_NOFOLLOW")
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=entry_id),
    ):
        with pytest.raises(MemoryIntegrityError, match="no-follow"):
            store.create(
                kind="preset",
                title="blocked",
                run_config=make_run_config(),
                folder=tmp_path,
            )

    assert list(store.presets_dir.glob("*.json")) == []


def test_hardlinked_lock_file_fails_closed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "4" * 12
    outside = tmp_path / "outside-hardlink"
    outside.write_text("lock", encoding="utf-8")
    os.link(outside, store.locks_dir / f"{entry_id}.lock")
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=entry_id),
    ):
        with pytest.raises(MemoryIntegrityError, match="private regular file"):
            store.create(
                kind="preset",
                title="blocked",
                run_config=make_run_config(),
                folder=tmp_path,
            )

    assert list(store.presets_dir.glob("*.json")) == []


def test_existing_lock_mode_is_normalized(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "5" * 12
    lock_path = store.locks_dir / f"{entry_id}.lock"
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o666)
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=entry_id),
    ):
        store.create(
            kind="preset",
            title="safe",
            run_config=make_run_config(),
            folder=tmp_path,
        )

    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_entry_symlink_is_never_loaded_or_listed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry = store.create(
        kind="preset",
        title="private",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    entry_path = store._entry_path(entry.kind, entry.id)
    outside = tmp_path / "outside-entry.json"
    entry_path.replace(outside)
    entry_path.symlink_to(outside)

    with pytest.raises(ValueError, match="unsafe"):
        store.load(entry.id, folder=tmp_path)
    assert store.list_entries(folder=tmp_path) == []
    assert outside.read_bytes()


def test_memory_directory_symlink_fails_closed(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    outside = tmp_path / "outside-memory"
    outside.mkdir()
    (state_root / "memory").symlink_to(outside, target_is_directory=True)

    with pytest.raises((MemoryIntegrityError, OSError)):
        MemoryStore(state_root)
    assert list(outside.iterdir()) == []


def test_entry_identity_mismatch_cannot_redirect_edit_or_delete(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    first = store.create(
        kind="preset",
        title="first",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    second = store.create(
        kind="preset",
        title="second",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    first_path = store._entry_path(first.kind, first.id)
    second_path = store._entry_path(second.kind, second.id)
    second_bytes = second_path.read_bytes()
    payload = json.loads(first_path.read_bytes())
    payload["id"] = second.id
    first_path.write_text(json.dumps(payload), encoding="utf-8")
    first_path.chmod(0o600)

    with pytest.raises(ValueError, match="identity differs"):
        store.edit(first.id, title="redirected", folder=tmp_path)
    with pytest.raises(ValueError, match="identity differs"):
        store.delete(first.id, folder=tmp_path)

    assert second_path.read_bytes() == second_bytes
    assert store.load(second.id, folder=tmp_path).title == "second"


def test_entry_kind_mismatch_is_rejected_and_skipped(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry = store.create(
        kind="preset",
        title="wrong kind",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    path = store._entry_path(entry.kind, entry.id)
    payload = json.loads(path.read_bytes())
    payload["kind"] = "history"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ValueError, match="identity differs"):
        store.load(entry.id, folder=tmp_path)
    assert store.list_entries(folder=tmp_path) == []


def test_nonprivate_entry_permissions_are_rejected(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry = store.create(
        kind="preset",
        title="private",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    path = store._entry_path(entry.kind, entry.id)
    path.chmod(0o644)

    with pytest.raises(ValueError, match="permissions are not private"):
        store.load(entry.id, folder=tmp_path)
    assert store.list_entries(folder=tmp_path) == []


def test_staging_cleanup_failure_keeps_committed_entry_readable(
    tmp_path: Path, monkeypatch
) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "6" * 12
    original_unlink = Path.unlink
    failed = False

    def fail_first_staging_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal failed
        if not failed and path.suffix == ".tmp":
            failed = True
            raise OSError("staging unlink failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_staging_unlink)
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=entry_id),
    ):
        entry = store.create(
            kind="preset",
            title="committed",
            run_config=make_run_config(),
            folder=tmp_path,
        )

    path = store._entry_path("preset", entry_id)
    staging_paths = list(store.presets_dir.glob("*.tmp"))
    assert entry.id == entry_id
    assert path.exists()
    assert len(staging_paths) == 1
    assert path.stat().st_ino == staging_paths[0].stat().st_ino
    assert store.load(entry_id, folder=tmp_path).title == "committed"
    assert not staging_paths[0].exists()


def test_persistent_staging_cleanup_failure_keeps_final_and_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "b" * 12
    original_unlink = Path.unlink

    def fail_staging_unlink(path: Path, *args, **kwargs) -> None:
        if path.suffix == ".tmp":
            raise OSError("persistent staging unlink failure")
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", fail_staging_unlink)
        with mock.patch(
            "ailoop.memory.uuid.uuid4",
            return_value=SimpleNamespace(hex=entry_id),
        ):
            store.create(
                kind="preset",
                title="committed",
                run_config=make_run_config(),
                folder=tmp_path,
            )
        with pytest.raises(MemoryStorageError, match="persistent staging unlink failure"):
            store.load(entry_id, folder=tmp_path)

    path = store._entry_path("preset", entry_id)
    assert path.exists()
    assert len(list(store.presets_dir.glob("*.tmp"))) == 1
    assert store.load(entry_id, folder=tmp_path).title == "committed"
    assert list(store.presets_dir.glob("*.tmp")) == []


def test_staging_cleanup_never_masks_an_unmanaged_hardlink(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry = store.create(
        kind="preset",
        title="private",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    path = store._entry_path(entry.kind, entry.id)
    staging = path.parent / f".{path.name}.stale.tmp"
    outside = tmp_path / "outside-entry-hardlink.json"
    os.link(path, staging)
    os.link(path, outside)
    original = path.read_bytes()

    with pytest.raises(MemoryIntegrityError, match="private regular file"):
        store.load(entry.id, folder=tmp_path)

    assert not staging.exists()
    assert path.read_bytes() == original
    assert outside.read_bytes() == original


def test_delete_removes_recoverable_staging_alias(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry = store.create(
        kind="preset",
        title="delete",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    path = store._entry_path(entry.kind, entry.id)
    staging = path.parent / f".{path.name}.stale.tmp"
    os.link(path, staging)

    store.delete(entry.id, folder=tmp_path)

    assert not path.exists()
    assert not staging.exists()


def test_postcommit_lock_cleanup_failure_does_not_report_create_failure(
    tmp_path: Path, monkeypatch
) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "c" * 12
    original_flock = fcntl.flock

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError("unlock failed")
        original_flock(descriptor, operation)

    monkeypatch.setattr("ailoop.memory.fcntl.flock", fail_unlock)
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=entry_id),
    ):
        entry = store.create(
            kind="preset",
            title="committed",
            run_config=make_run_config(),
            folder=tmp_path,
        )

    assert entry.id == entry_id
    assert store.load(entry_id, folder=tmp_path).title == "committed"


def test_postcommit_directory_fsync_failure_keeps_published_entry(
    tmp_path: Path, monkeypatch
) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "7" * 12

    def fail_directory_fsync(_path: Path) -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(MemoryStore, "_fsync_directory", staticmethod(fail_directory_fsync))
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=entry_id),
    ):
        entry = store.create(
            kind="preset",
            title="committed",
            run_config=make_run_config(),
            folder=tmp_path,
        )

    assert entry.id == entry_id
    assert store.load(entry_id, folder=tmp_path).title == "committed"


def test_failed_staging_write_publishes_nothing(tmp_path: Path, monkeypatch) -> None:
    store = MemoryStore(tmp_path)
    entry_id = "1" * 12

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr("ailoop.memory.os.fsync", fail_fsync)
    with mock.patch(
        "ailoop.memory.uuid.uuid4",
        return_value=SimpleNamespace(hex=entry_id),
    ):
        with pytest.raises(OSError, match="fsync failed"):
            store.create(
                kind="preset",
                title="not published",
                run_config=make_run_config(),
                folder=tmp_path,
            )

    assert not store._entry_path("preset", entry_id).exists()
    assert list(store.presets_dir.glob("*.tmp")) == []


def test_concurrent_identical_candidate_creations_remain_unique(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    barrier = threading.Barrier(2)
    counter_lock = threading.Lock()
    calls = 0

    def generate_uuid() -> SimpleNamespace:
        nonlocal calls
        with counter_lock:
            index = calls
            calls += 1
        if index < 2:
            barrier.wait(timeout=5)
            return SimpleNamespace(hex="2" * 12)
        return SimpleNamespace(hex=f"{index + 1:012x}")

    def create(title: str):
        return store.create(
            kind="preset",
            title=title,
            run_config=make_run_config(),
            folder=tmp_path,
        )

    with mock.patch("ailoop.memory.uuid.uuid4", side_effect=generate_uuid):
        with ThreadPoolExecutor(max_workers=2) as executor:
            created = list(executor.map(create, ("first", "second")))

    assert len({entry.id for entry in created}) == 2
    assert "2" * 12 in {entry.id for entry in created}
    assert sorted(entry.title for entry in store.list_entries(folder=tmp_path)) == [
        "first",
        "second",
    ]


@pytest.mark.parametrize(
    ("first_kind", "second_kind"),
    [("preset", "preset"), ("preset", "history")],
)
def test_separate_process_candidate_collisions_remain_globally_unique(
    tmp_path: Path,
    first_kind: str,
    second_kind: str,
) -> None:
    MemoryStore(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    collision_id = "8" * 12
    processes = [
        context.Process(
            target=create_memory_in_process,
            args=(
                str(tmp_path),
                str(tmp_path),
                kind,
                title,
                collision_id,
                replacement_id,
                ready,
                start,
                results,
            ),
        )
        for kind, title, replacement_id in (
            (first_kind, "first", "9" * 12),
            (second_kind, "second", "a" * 12),
        )
    ]

    try:
        for process in processes:
            process.start()
        assert {ready.get(timeout=15), ready.get(timeout=15)} == {"first", "second"}
        start.set()
        outcomes = [results.get(timeout=15), results.get(timeout=15)]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
    ids = {outcome[1] for outcome in outcomes}
    assert len(ids) == 2
    assert collision_id in ids
    assert sorted(entry.title for entry in MemoryStore(tmp_path).list_entries(folder=tmp_path)) == [
        "first",
        "second",
    ]


def test_legacy_cross_kind_duplicate_fails_closed_for_mutations(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry = store.create(
        kind="preset",
        title="preset",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    preset_path = store._entry_path("preset", entry.id)
    duplicate = deepcopy(entry)
    duplicate.kind = "history"
    duplicate.title = "history"
    history_path = store._entry_path("history", entry.id)
    history_path.write_text(json.dumps(duplicate.to_dict()), encoding="utf-8")
    history_path.chmod(0o600)
    original = (preset_path.read_bytes(), history_path.read_bytes())

    with pytest.raises(MemoryIntegrityError, match="multiple kinds"):
        store.load(entry.id, folder=tmp_path)
    with pytest.raises(MemoryIntegrityError, match="multiple kinds"):
        store.edit(entry.id, title="unsafe", folder=tmp_path)
    with pytest.raises(MemoryIntegrityError, match="multiple kinds"):
        store.delete(entry.id, folder=tmp_path)
    with pytest.raises(MemoryIntegrityError, match="multiple kinds"):
        store.mark_used(entry.id, folder=tmp_path)
    updated = deepcopy(entry)
    updated.title = "unsafe save"
    with pytest.raises(MemoryIntegrityError, match="multiple kinds"):
        store.save(updated)

    assert (preset_path.read_bytes(), history_path.read_bytes()) == original
    assert store.list_entries(folder=tmp_path) == []
