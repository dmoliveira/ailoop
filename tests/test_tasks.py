from pathlib import Path

import pytest

from ailoop.tasks import TASK_FILE_TEMPLATE, TaskFileError, parse_task_file, render_task_file_error


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


def test_parse_task_file_rejects_duplicate_title(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    with pytest.raises(ValueError, match="Duplicate task file title"):
        parse_task_file(path)


def test_parse_task_file_rejects_duplicate_section(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n"
        "## To do\n- None\n\n"
        "## To do\n- None\n\n"
        "## Doing\n- None\n\n"
        "## Done\n- None\n"
    )
    with pytest.raises(ValueError, match="Duplicate task section: To do"):
        parse_task_file(path)


def test_parse_task_file_rejects_duplicate_none(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- None\n- None\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    with pytest.raises(ValueError, match="Duplicate - None in To do"):
        parse_task_file(path)


def test_parse_task_file_title_must_be_before_sections(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "## To do\n- None\n\n# Loop Tasks\n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    with pytest.raises(ValueError, match="Title must be before task sections"):
        parse_task_file(path)


def test_parse_task_file_rejects_too_many_doing(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- [ ] One\n- [ ] Two\n\n## Done\n- None\n"
    )
    with pytest.raises(ValueError, match="Too many tasks in Doing: 2 > 1"):
        parse_task_file(path)


def test_render_task_file_error_is_friendly(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    text = render_task_file_error(path, ValueError("Broken task file"))
    assert f"bad task file: {path}" in text
    assert "Broken task file" in text
    assert "task-template --with-rules" in text


def test_render_task_file_error_includes_line_context(tmp_path: Path) -> None:
    path = tmp_path / "bad.md"
    text = render_task_file_error(
        path,
        TaskFileError("Invalid task line in To do: - bad", line_number=4, line_text="- bad"),
    )
    assert "line 4" in text
    assert "Invalid task line in To do" in text
    assert "content: - bad" in text


def test_parse_task_file_preserves_line_number_for_inline_errors(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text("# Loop Tasks\n\n## To do\n- bad\n\n## Doing\n- None\n\n## Done\n- None\n")
    with pytest.raises(TaskFileError) as exc:
        parse_task_file(path)
    assert exc.value.line_number == 4
    assert exc.value.line_text == "- bad"


def test_parse_task_file_rejects_empty_open_task(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- [ ] \n\n## Doing\n- None\n\n## Done\n- None\n"
    )
    with pytest.raises(ValueError, match="Empty task item in To do"):
        parse_task_file(path)


def test_parse_task_file_rejects_empty_done_task(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text(
        "# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- None\n\n## Done\n- [x] \n"
    )
    with pytest.raises(ValueError, match="Empty task item in Done"):
        parse_task_file(path)


def test_parse_task_file_rejects_missing_space_after_open_checkbox(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text("# Loop Tasks\n\n## To do\n- [ ]Task\n\n## Doing\n- None\n\n## Done\n- None\n")
    with pytest.raises(TaskFileError, match="Invalid task line in To do"):
        parse_task_file(path)


def test_parse_task_file_rejects_missing_space_after_done_checkbox(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    path.write_text("# Loop Tasks\n\n## To do\n- None\n\n## Doing\n- None\n\n## Done\n- [x]Done\n")
    with pytest.raises(TaskFileError, match="Invalid task line in Done"):
        parse_task_file(path)
