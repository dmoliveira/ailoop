from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

TASK_FILE_TEMPLATE = """# Loop Tasks

## To do
- [ ] First task

## Doing
- None

## Done
- None
"""

TASK_FILE_RULES = """Task file rules:
- use sections: To do, Doing, Done
- To do: only - [ ] task or - None
- Doing: only - [ ] task or - None
- Done: only - [x] task or - None
- keep max 1 task in Doing
- move task To do -> Doing when start
- move task Doing -> Done and mark [x] when done
- use - None when a section is empty
- update the file before you end
""".strip()

TASK_FILE_GUIDE = f"{TASK_FILE_TEMPLATE.rstrip()}\n\n{TASK_FILE_RULES}\n"


@dataclass(slots=True)
class TaskFileState:
    todo: list[str]
    doing: list[str]
    done: list[str]

    @property
    def is_complete(self) -> bool:
        return not self.todo and not self.doing

    def to_dict(self) -> dict[str, object]:
        return {
            "todo": self.todo,
            "doing": self.doing,
            "done": self.done,
            "todo_count": len(self.todo),
            "doing_count": len(self.doing),
            "done_count": len(self.done),
            "is_complete": self.is_complete,
        }


def render_task_file_check(state: TaskFileState) -> str:
    status = "✅ complete" if state.is_complete else "⏳ open"
    return "\n".join(
        [
            f"{status}",
            f"↳ to do {len(state.todo)} · doing {len(state.doing)} · done {len(state.done)}",
        ]
    )


def render_task_file_check_verbose(state: TaskFileState) -> str:
    def block(title: str, items: list[str]) -> list[str]:
        if not items:
            return [title, "- None"]
        return [title, *[f"- {item}" for item in items]]

    lines = [render_task_file_check(state), ""]
    lines.extend(block("To do:", state.todo))
    lines.append("")
    lines.extend(block("Doing:", state.doing))
    lines.append("")
    lines.extend(block("Done:", state.done))
    return "\n".join(lines)


def parse_task_file(path: Path, max_doing: int = 1) -> TaskFileState:
    if not path.exists():
        raise FileNotFoundError(f"Task file not found: {path}")

    sections = {"To do": [], "Doing": [], "Done": []}
    none_flags = {"To do": False, "Doing": False, "Done": False}
    current: str | None = None
    seen: set[str] = set()
    saw_title = False

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "# Loop Tasks":
            if current is not None:
                raise ValueError("Title must be before task sections")
            if saw_title:
                raise ValueError("Duplicate task file title")
            saw_title = True
            continue
        if line.startswith("## "):
            name = line[3:].strip()
            if name in sections:
                if name in seen:
                    raise ValueError(f"Duplicate task section: {name}")
                current = name
                seen.add(name)
                continue
            raise ValueError(f"Unknown task section: {name}")
        if current is None:
            raise ValueError(f"Unexpected content outside task sections: {line}")
        if line == "- None":
            if sections[current]:
                raise ValueError(f"Cannot mix - None with tasks in {current}")
            if none_flags[current]:
                raise ValueError(f"Duplicate - None in {current}")
            none_flags[current] = True
            continue
        if current in {"To do", "Doing"}:
            if none_flags[current]:
                raise ValueError(f"Cannot mix tasks with - None in {current}")
            if not line.startswith("- [ ] "):
                raise ValueError(f"Invalid task line in {current}: {line}")
            sections[current].append(line[6:].strip())
            continue
        if current == "Done":
            if none_flags[current]:
                raise ValueError("Cannot mix tasks with - None in Done")
            if not line.startswith("- [x] "):
                raise ValueError(f"Invalid task line in Done: {line}")
            sections[current].append(line[6:].strip())

    missing = [name for name in sections if name not in seen]
    if missing:
        raise ValueError(f"Missing task sections: {', '.join(missing)}")
    if not saw_title:
        raise ValueError("Missing task file title: # Loop Tasks")
    empty_without_none = [
        name for name, items in sections.items() if not items and not none_flags[name]
    ]
    if empty_without_none:
        raise ValueError(f"Empty sections must use - None: {', '.join(empty_without_none)}")
    if len(sections["Doing"]) > max_doing:
        raise ValueError(f"Too many tasks in Doing: {len(sections['Doing'])} > {max_doing}")

    return TaskFileState(
        todo=sections["To do"],
        doing=sections["Doing"],
        done=sections["Done"],
    )
