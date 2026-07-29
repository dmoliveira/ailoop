from __future__ import annotations

import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from ailoop.memory import MAX_MEMORY_ID_ATTEMPTS, MemoryStore
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


def test_public_save_rejects_overwrite_and_cross_kind_collision(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    entry = store.create(
        kind="preset",
        title="existing",
        run_config=make_run_config(),
        folder=tmp_path,
    )
    path = store._entry_path(entry.kind, entry.id)
    original = path.read_bytes()

    with pytest.raises(FileExistsError, match="already exists"):
        store.save(deepcopy(entry))
    duplicate = deepcopy(entry)
    duplicate.kind = "history"
    with pytest.raises(FileExistsError, match="already exists"):
        store.save(duplicate)

    assert path.read_bytes() == original
    assert not store._entry_path("history", entry.id).exists()


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
        with pytest.raises(RuntimeError, match="no-follow"):
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
        with pytest.raises(RuntimeError, match="regular file"):
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

    with pytest.raises((OSError, RuntimeError)):
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


def test_staging_cleanup_failure_rolls_back_publication(tmp_path: Path, monkeypatch) -> None:
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
        with pytest.raises(OSError, match="staging unlink failed"):
            store.create(
                kind="preset",
                title="rolled back",
                run_config=make_run_config(),
                folder=tmp_path,
            )

    assert not store._entry_path("preset", entry_id).exists()
    assert list(store.presets_dir.glob("*.tmp")) == []


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
