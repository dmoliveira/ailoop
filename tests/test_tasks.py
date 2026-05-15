from pathlib import Path

import pytest

from ailoop.tasks import TASK_FILE_TEMPLATE, parse_task_file


def test_parse_task_file_reads_valid_template(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(TASK_FILE_TEMPLATE)
    state = parse_task_file(path)
    assert state.todo == ["First task"]
    assert state.doing == []
    assert state.done == []
    assert state.is_complete is False


def test_parse_task_file_detects_complete_state(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- None\n\n## Done\n- [x] Done item\n"
    )
    state = parse_task_file(path)
    assert state.is_complete is True


def test_parse_task_file_rejects_bad_shape(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text("# Loop Tasks\n\n## To do\n- bad\n\n## Doing\n- None\n\n## Done\n- None\n")
    with pytest.raises(ValueError):
        parse_task_file(path)


def test_parse_task_file_rejects_unknown_section(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- None\n\n## Extra\n- None\n\n"
        "## Doing\n- None\n\n## Done\n- None\n"
    )
    with pytest.raises(ValueError):
        parse_task_file(path)


def test_parse_task_file_rejects_none_mixed_with_tasks(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- None\n- [ ] Task\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    with pytest.raises(ValueError):
        parse_task_file(path)


def test_parse_task_file_rejects_stray_content(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\nOops\n\n## To do\n- None\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    with pytest.raises(ValueError):
        parse_task_file(path)


def test_parse_task_file_requires_title(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text("## To do\n- None\n\n## Doing\n- None\n\n## Done\n- None\n")
    with pytest.raises(ValueError):
        parse_task_file(path)


def test_parse_task_file_requires_none_for_empty_sections(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text("# Loop Tasks\n\n## To do\n\n## Doing\n- None\n\n## Done\n- None\n")
    with pytest.raises(ValueError):
        parse_task_file(path)
