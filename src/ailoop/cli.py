from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import default_config_path, init_config_text, load_app_config, resolve_run_config
from .memory import MemoryStore, render_memory_list, render_memory_show, run_config_from_entry
from .paths import ensure_dir
from .service import LoopService
from .stats import get_color_mode, render_loop_list, render_stats, render_status, set_color_mode
from .tasks import (
    TASK_FILE_GUIDE,
    TASK_FILE_TEMPLATE,
    parse_task_file,
    render_task_file_check,
    render_task_file_check_verbose,
)
from .tui import launch_in_tmux, run_tui

TOP_LEVEL_DESCRIPTION = "Run AI terminal tools in repeatable, resumable loops."

TOP_LEVEL_EPILOG = """
Logical groups:
  Setup:
    init-config    Write ~/.config/ailoop/config.yaml
    init-task-file Create a task file from the template
    task-template  Print task file template
    check-task-file Validate a task file

  Loop execution:
    run            Start a new loop
    resume         Continue a paused or stopped loop
    replay         Run a saved preset or history entry

  Memory:
    memory save    Save a preset or history entry
    memory list    List saved memory entries
    memory show    Show one memory entry
    memory edit    Update a saved memory entry
    memory favorite Mark or unmark a favorite entry
    memory delete  Delete a saved memory entry

  Loop control:
    pause          Ask a running loop to pause after the current iteration
    stop           Ask a running loop to stop after the current iteration

  Inspection:
    list           Show known loops and their current status
    ps             Short alias for: list --running
    tui            Open the interactive dashboard
    status         Show one loop snapshot
    stats          Show one loop snapshot + recent history
    logs           Show log file paths or contents
    tail           Show the last log lines

  Cleanup:
    remove         Delete saved state/logs for a loop

Common usage:
  ailoop run "Review the repo" --runner opencode --agent orchestrator
  ailoop memory save "Quick review" "Review the repo" --runner opencode
  ailoop replay <memory-id>
  ailoop tui
  ailoop ps
  ailoop status <loop-id>
  ailoop pause <loop-id>
  ailoop run "Do 5 iterations" --steps 5
  ailoop task-template > loop_tasks.md
  ailoop run "Work tasks" --task-file ./loop_tasks.md --until-tasks-complete
  ailoop check-task-file ./loop_tasks.md
""".strip()

RUNNING_STATUSES = {"running", "pause_requested", "stop_requested"}
ACTIVE_STATUSES = RUNNING_STATUSES | {"paused", "idle"}


def _read_log_excerpt(path: Path, lines: int) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Log file not found: {path}")
    content = path.read_text().splitlines()
    return "\n".join(content[-lines:])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ailoop",
        description=TOP_LEVEL_DESCRIPTION,
        epilog=TOP_LEVEL_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Path to config file (default: ~/.config/ailoop/config.yaml)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON when supported")
    parser.add_argument("--quiet", action="store_true", help="Print less output")
    parser.add_argument("--verbose", action="store_true", help="Print more detail")
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Color mode for text output",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init-config",
        help="Write the default config file",
        description="Write the default config file to ~/.config/ailoop/config.yaml.",
    )
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing config")

    init_task_parser = subparsers.add_parser(
        "init-task-file",
        help="Create a task file",
        description="Create a task file from the built-in template.",
    )
    init_task_parser.add_argument("path", type=Path, help="Path to write")
    init_task_parser.add_argument("--force", action="store_true", help="Overwrite an existing file")

    subparsers.add_parser(
        "task-template",
        help="Print task file template",
        description=(
            "Print the task file template. Use --with-rules for the short guide. "
            "Example: ailoop task-template > loop_tasks.md"
        ),
    ).add_argument(
        "--with-rules",
        action="store_true",
        help="Print the template plus the short guide",
    )

    check_task_parser = subparsers.add_parser(
        "check-task-file",
        help="Validate a task file",
        description="Validate the strict task file format and print a short summary.",
    )
    check_task_parser.add_argument("path", type=Path, help="Task file path")
    check_task_parser.add_argument(
        "--state-exit-code",
        action="store_true",
        help="Exit 0 if done, 10 if valid but open, 1 if invalid",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Start a new loop",
        description=(
            "Start a new loop. By default it runs forever unless --steps is set.\n\n"
            "Examples:\n"
            "  ailoop run \"Review the repo\" --runner opencode --agent orchestrator\n"
            "  ailoop run \"Do 5 iterations\" --steps 5\n"
            "  ailoop task-template > loop_tasks.md\n"
            "  ailoop run \"Work tasks\" --task-file ./loop_tasks.md --until-tasks-complete"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_parser.add_argument("prompt", help="Main user prompt for each iteration")

    run_exec = run_parser.add_argument_group("Execution")
    run_exec.add_argument("--runner", help="Runner name from config, for example: opencode")
    run_exec.add_argument("--agent", help="Agent override, for example: orchestrator")
    run_exec.add_argument(
        "--steps",
        type=int,
        help="Run exactly N iterations. Omit to run forever.",
    )
    run_exec.add_argument(
        "--pause-seconds",
        type=int,
        help="Seconds to sleep between iterations",
    )
    run_exec.add_argument("--loop-id", help="Optional custom loop id")

    run_prompt = run_parser.add_argument_group("Prompt assembly")
    run_prompt.add_argument(
        "--no-pre-prompt",
        action="store_true",
        help="Disable the configured pre-prompt",
    )
    run_prompt.add_argument(
        "--no-agent-file",
        action="store_true",
        help="Do not attach AGENTS.md / instruction file contents",
    )
    run_prompt.add_argument(
        "--agent-file",
        help="Override the AGENTS.md / instruction file path",
    )
    run_prompt.add_argument("--task-file", help="Task file path")
    run_prompt.add_argument(
        "--until-tasks-complete",
        action="store_true",
        help="Stop when To do and Doing are empty in the task file",
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume a loop",
        description="Resume a previously paused or stopped loop.",
    )
    resume_parser.add_argument("loop_id", help="Loop id to resume")

    replay_parser = subparsers.add_parser(
        "replay",
        help="Replay a saved memory entry",
        description="Create and run a new loop from a saved preset or history entry.",
    )
    replay_parser.add_argument("entry_id", help="Saved memory entry id")
    replay_parser.add_argument("--loop-id", help="Optional custom loop id")

    memory_parser = subparsers.add_parser(
        "memory",
        help="Manage saved presets and history entries",
        description="Save, inspect, update, and reuse saved presets/history entries.",
    )
    memory_subparsers = memory_parser.add_subparsers(dest="memory_command", required=True)

    memory_save = memory_subparsers.add_parser(
        "save",
        help="Save a memory entry",
        description="Save a preset or history entry from run-style configuration flags.",
    )
    memory_save.add_argument("title", help="Entry title")
    memory_save.add_argument("prompt", help="Prompt to save")
    memory_save.add_argument("--kind", choices=["preset", "history"], default="preset")
    memory_save.add_argument("--runner", help="Runner name override")
    memory_save.add_argument("--agent", help="Agent override")
    memory_save.add_argument("--steps", type=int, help="Saved step limit")
    memory_save.add_argument("--pause-seconds", type=int, help="Saved pause between iterations")
    memory_save.add_argument(
        "--no-pre-prompt",
        action="store_true",
        help="Disable the configured pre-prompt",
    )
    memory_save.add_argument(
        "--no-agent-file",
        action="store_true",
        help="Do not attach AGENTS.md / instruction file contents",
    )
    memory_save.add_argument("--agent-file", help="Override the AGENTS.md / instruction file path")
    memory_save.add_argument("--task-file", help="Saved task file path")
    memory_save.add_argument(
        "--until-tasks-complete",
        action="store_true",
        help="Stop when To do and Doing are empty in the task file",
    )
    memory_save.add_argument(
        "--label",
        action="append",
        default=[],
        help="Label to attach (repeatable)",
    )
    memory_save.add_argument("--favorite", action="store_true", help="Mark the entry as favorite")

    memory_list = memory_subparsers.add_parser(
        "list",
        help="List memory entries",
        description="List saved presets and history entries.",
    )
    memory_list.add_argument("--kind", choices=["all", "preset", "history"], default="all")
    memory_list.add_argument("--favorites", action="store_true", help="Show only favorite entries")
    memory_list.add_argument(
        "--all-folders",
        action="store_true",
        help="Show entries from all folders for the current user",
    )

    memory_show = memory_subparsers.add_parser(
        "show",
        help="Show one memory entry",
        description="Show one saved preset or history entry.",
    )
    memory_show.add_argument("entry_id", help="Saved memory entry id")

    memory_edit = memory_subparsers.add_parser(
        "edit",
        help="Edit a memory entry",
        description="Update metadata or saved run configuration for a memory entry.",
    )
    memory_edit.add_argument("entry_id", help="Saved memory entry id")
    memory_edit.add_argument("--title", help="New title")
    memory_edit.add_argument("--prompt", help="New saved prompt")
    memory_edit.add_argument("--runner", help="New runner name")
    memory_edit.add_argument("--agent", help="New agent override")
    memory_edit.add_argument("--steps", type=int, help="New saved step limit")
    memory_edit.add_argument("--pause-seconds", type=int, help="New saved pause between iterations")
    memory_edit.add_argument(
        "--no-pre-prompt",
        action="store_true",
        help="Disable the configured pre-prompt",
    )
    memory_edit.add_argument(
        "--no-agent-file",
        action="store_true",
        help="Do not attach AGENTS.md / instruction file contents",
    )
    memory_edit.add_argument("--agent-file", help="Override the AGENTS.md / instruction file path")
    memory_edit.add_argument("--task-file", help="New saved task file path")
    memory_edit.add_argument(
        "--until-tasks-complete",
        action="store_true",
        help="Stop when To do and Doing are empty in the task file",
    )
    memory_edit.add_argument(
        "--label",
        action="append",
        default=None,
        help="Replace labels with repeated values",
    )
    memory_edit.add_argument(
        "--favorite",
        dest="favorite",
        action="store_true",
        help="Mark as favorite",
    )
    memory_edit.add_argument(
        "--no-favorite",
        dest="favorite",
        action="store_false",
        help="Remove favorite mark",
    )
    memory_edit.set_defaults(favorite=None)
    memory_edit.add_argument("--change-note", help="Short note for the new version snapshot")

    memory_favorite = memory_subparsers.add_parser(
        "favorite",
        help="Mark or unmark favorite",
        description="Toggle the favorite state for a memory entry.",
    )
    memory_favorite.add_argument("entry_id", help="Saved memory entry id")
    memory_favorite.add_argument("--off", action="store_true", help="Remove favorite mark")

    memory_delete = memory_subparsers.add_parser(
        "delete",
        help="Delete a memory entry",
        description="Delete a saved preset or history entry.",
    )
    memory_delete.add_argument("entry_id", help="Saved memory entry id")

    pause_parser = subparsers.add_parser(
        "pause",
        help="Pause a running loop",
        description="Ask a running loop to pause after the current iteration completes.",
    )
    pause_parser.add_argument("loop_id", help="Loop id to pause")

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop a running loop",
        description="Ask a running loop to stop after the current iteration completes.",
    )
    stop_parser.add_argument("loop_id", help="Loop id to stop")

    list_parser = subparsers.add_parser(
        "list",
        help="List known loops",
        description="List known loops from ~/.config/ailoop/state.",
    )
    list_group = list_parser.add_mutually_exclusive_group()
    list_group.add_argument(
        "--active",
        action="store_true",
        help="Show loops that still need attention: running, requested, paused, or idle",
    )
    list_group.add_argument(
        "--running",
        action="store_true",
        help="Show only loops that are still executing or about to stop/pause",
    )
    list_group.add_argument(
        "--all",
        action="store_true",
        help="Show all loops (default)",
    )

    tui_parser = subparsers.add_parser(
        "tui",
        help="Open the interactive dashboard",
        description="Open the interactive dashboard with loops, logs, and controls.",
    )
    tui_parser.add_argument("--loop-id", help="Preselect a loop id")
    tui_parser.add_argument(
        "--tmux",
        action="store_true",
        help="Run the dashboard inside a tmux session",
    )
    tui_parser.add_argument("--tmux-session", action="store_true", help=argparse.SUPPRESS)

    subparsers.add_parser(
        "ps",
        help="Alias for: list --running",
        description="Show running loops. Use this first when you want to pause or stop one.",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Show loop status",
        description="Show the latest summary for one loop.",
    )
    status_parser.add_argument("loop_id", help="Loop id to inspect")

    stats_parser = subparsers.add_parser(
        "stats",
        help="Show loop stats",
        description="Show loop summary plus recent iteration history.",
    )
    stats_parser.add_argument("loop_id", help="Loop id to inspect")

    logs_parser = subparsers.add_parser(
        "logs",
        help="Show loop log files",
        description="Show loop log file paths, or print their contents.",
    )
    logs_parser.add_argument("loop_id", help="Loop id to inspect")
    logs_parser.add_argument("--iteration", type=int, help="Iteration number to inspect")
    logs_parser.add_argument(
        "--kind",
        choices=["prompt", "stdout", "stderr", "all"],
        default="all",
        help="Which log kind to show",
    )
    logs_parser.add_argument(
        "--print",
        action="store_true",
        dest="print_content",
        help="Print log contents instead of only file paths",
    )

    tail_parser = subparsers.add_parser(
        "tail",
        help="Tail a loop log",
        description="Tail the latest or selected iteration log for a loop.",
    )
    tail_parser.add_argument("loop_id", help="Loop id to inspect")
    tail_parser.add_argument("--iteration", type=int, help="Iteration number to inspect")
    tail_parser.add_argument(
        "--kind",
        choices=["stdout", "stderr", "prompt"],
        default="stdout",
        help="Which log kind to tail",
    )
    tail_parser.add_argument("-n", "--lines", type=int, default=40, help="Number of lines to show")

    remove_parser = subparsers.add_parser(
        "remove",
        help="Delete a saved loop",
        description=(
            "Delete saved state and logs for one loop. "
            "Locked/running loops cannot be removed."
        ),
    )
    remove_parser.add_argument("loop_id", help="Loop id to delete")
    remove_parser.add_argument(
        "--force",
        action="store_true",
        help="Allow deleting non-locked active loops such as paused or idle ones",
    )

    return parser


def write_init_config(config_path: Path, force: bool) -> None:
    ensure_dir(config_path.parent)
    if config_path.exists() and not force:
        raise SystemExit(f"Config already exists: {config_path}. Use --force to overwrite.")
    config_path.write_text(init_config_text())
    print(f"✅ wrote config: {config_path}")
    print('↳ next: ailoop run "Review the repo"')


def write_task_file(path: Path, force: bool) -> None:
    ensure_dir(path.parent)
    if path.exists() and not force:
        raise SystemExit(f"Task file already exists: {path}. Use --force to overwrite.")
    path.write_text(TASK_FILE_TEMPLATE)
    print(f"📝 wrote task file: {path}")
    print(f"↳ next: ailoop check-task-file {path}")


def print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2))


def friendly_task_file_error(path: Path, exc: Exception) -> str:
    return "\n".join(
        [
            f"❌ bad task file: {path}",
            f"↳ {exc}",
            "↳ tip: ailoop task-template --with-rules",
        ]
    )


def normalize_global_args(argv: list[str]) -> list[str]:
    if "--config" not in argv:
        return argv

    items = list(argv)
    index = items.index("--config")
    if index + 1 >= len(items):
        return items
    pair = [items[index], items[index + 1]]
    del items[index : index + 2]
    return pair + items


def _resolve_memory_run_config(args: argparse.Namespace, app_config) -> object:
    return resolve_run_config(
        app_config,
        prompt=args.prompt,
        runner=args.runner,
        agent=args.agent,
        steps=args.steps,
        pause_seconds=args.pause_seconds,
        pre_prompt_enabled=False if getattr(args, "no_pre_prompt", False) else None,
        attach_agent_file=False if getattr(args, "no_agent_file", False) else None,
        agent_file=args.agent_file,
        task_file=args.task_file,
        stop_when_tasks_complete=True if getattr(args, "until_tasks_complete", False) else None,
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args(normalize_global_args(sys.argv[1:]))
    previous_color_mode = get_color_mode()
    set_color_mode(args.color)
    try:
        if args.command == "init-config":
            write_init_config(args.config, args.force)
            return

        if args.command == "init-task-file":
            write_task_file(args.path.expanduser(), args.force)
            return

        if args.command == "task-template":
            print(TASK_FILE_GUIDE.rstrip() if args.with_rules else TASK_FILE_TEMPLATE.rstrip())
            return

        if args.command == "tui":
            if args.tmux and not args.tmux_session and not os.environ.get("TMUX"):
                launch_in_tmux(args.config, loop_id=args.loop_id)
                return
            run_tui(args.config, loop_id=args.loop_id)
            return

        if args.command == "check-task-file":
            path = args.path.expanduser().resolve()
            try:
                task_state = parse_task_file(path)
            except Exception as exc:
                if args.json:
                    print_json({"ok": False, "path": str(path), "error": str(exc)})
                else:
                    print(friendly_task_file_error(path, exc))
                raise SystemExit(1) from exc
            if args.json:
                print_json({"ok": True, "path": str(path), **task_state.to_dict()})
            elif args.quiet:
                pass
            elif args.verbose:
                print(render_task_file_check_verbose(task_state))
            else:
                print(render_task_file_check(task_state))
            if args.state_exit_code:
                raise SystemExit(0 if task_state.is_complete else 10)
            return

        app_config = load_app_config(args.config)
        service = LoopService(
            Path(app_config.paths.state_dir),
            emit_output=not (args.quiet or args.json),
        )
        memory = MemoryStore(Path(app_config.paths.state_dir))

        if args.command == "memory":
            if args.memory_command == "save":
                run_config = _resolve_memory_run_config(args, app_config)
                entry = memory.create(
                    kind=args.kind,
                    title=args.title,
                    run_config=run_config,
                    folder=Path.cwd(),
                    labels=args.label,
                    favorite=args.favorite,
                    source_command="memory save",
                )
                if args.json:
                    print_json(entry.to_dict())
                elif not args.quiet:
                    print(render_memory_show(entry))
                return

            if args.memory_command == "list":
                entries = memory.list_entries(
                    kind=None if args.kind == "all" else args.kind,
                    favorites_only=args.favorites,
                    all_folders=args.all_folders,
                    folder=Path.cwd(),
                )
                if args.json:
                    print_json([entry.to_dict() for entry in entries])
                elif not args.quiet:
                    print(render_memory_list(entries))
                return

            if args.memory_command == "show":
                entry = memory.load(args.entry_id, folder=Path.cwd())
                if args.json:
                    print_json(entry.to_dict())
                elif not args.quiet:
                    print(render_memory_show(entry))
                return

            if args.memory_command == "edit":
                updates_requested = any(
                    value is not None
                    for value in (
                        args.prompt,
                        args.runner,
                        args.agent,
                        args.steps,
                        args.pause_seconds,
                        args.agent_file,
                        args.task_file,
                    )
                ) or args.no_pre_prompt or args.no_agent_file or args.until_tasks_complete
                run_config = (
                    _resolve_memory_run_config(args, app_config) if updates_requested else None
                )
                entry = memory.edit(
                    args.entry_id,
                    run_config=run_config,
                    title=args.title,
                    labels=args.label,
                    favorite=args.favorite,
                    change_note=args.change_note,
                    folder=Path.cwd(),
                )
                if args.json:
                    print_json(entry.to_dict())
                elif not args.quiet:
                    print(render_memory_show(entry))
                return

            if args.memory_command == "favorite":
                entry = memory.edit(args.entry_id, favorite=not args.off, folder=Path.cwd())
                if args.json:
                    print_json(entry.to_dict())
                elif not args.quiet:
                    print(render_memory_show(entry))
                return

            if args.memory_command == "delete":
                memory.delete(args.entry_id, folder=Path.cwd())
                if args.json:
                    print_json({"ok": True, "deleted": args.entry_id})
                elif not args.quiet:
                    print(f"Deleted memory entry: {args.entry_id}")
                return

        if args.command == "run":
            run_config = _resolve_memory_run_config(args, app_config)
            state = service.create_loop(run_config, loop_id=args.loop_id)
            if not args.quiet and not args.json:
                print(f"Loop ID: {state.loop_id}")
            final_state = service.run_loop(state.loop_id)
            if args.json:
                print_json(final_state.to_dict())
            elif not args.quiet:
                print(render_status(final_state))
            return

        if args.command == "resume":
            final_state = service.run_loop(args.loop_id)
            if args.json:
                print_json(final_state.to_dict())
            elif not args.quiet:
                print(render_status(final_state))
            return

        if args.command == "replay":
            entry = memory.load(args.entry_id, folder=Path.cwd())
            run_config = run_config_from_entry(entry, app_config)
            state = service.create_loop(run_config, loop_id=args.loop_id)
            if not args.quiet and not args.json:
                print(f"Loop ID: {state.loop_id}")
            final_state = service.run_loop(state.loop_id)
            memory.mark_used(args.entry_id, folder=Path.cwd())
            if args.json:
                print_json(final_state.to_dict())
            elif not args.quiet:
                print(render_status(final_state))
            return

        if args.command == "pause":
            state = service.request_control(args.loop_id, "pause")
            if args.json:
                print_json(state.to_dict())
            elif not args.quiet:
                print(render_status(state))
            return

        if args.command == "stop":
            state = service.request_control(args.loop_id, "stop")
            if args.json:
                print_json(state.to_dict())
            elif not args.quiet:
                print(render_status(state))
            return

        if args.command in {"list", "ps"}:
            states = service.list_loops()
            if args.command == "ps":
                states = [state for state in states if state.status in RUNNING_STATUSES]
            elif args.running:
                states = [state for state in states if state.status in RUNNING_STATUSES]
            elif args.active:
                states = [state for state in states if state.status in ACTIVE_STATUSES]
            if args.json:
                print_json([state.to_dict() for state in states])
            else:
                print(render_loop_list(states))
            return

        if args.command in {"status", "stats"}:
            state = service.load_loop(args.loop_id)
            if args.json:
                payload = state.to_dict()
                if args.command == "stats":
                    payload["recent_iterations"] = [
                        item.to_dict() for item in state.iterations[-5:]
                    ]
                print_json(payload)
            else:
                print(render_stats(state) if args.command == "stats" else render_status(state))
            return

        if args.command == "logs":
            paths = service.loop_paths(args.loop_id, iteration=args.iteration)
            kinds = [args.kind] if args.kind != "all" else ["prompt", "stdout", "stderr"]
            if args.json:
                payload = {}
                for kind in kinds:
                    path = paths[kind]
                    payload[kind] = {
                        "path": str(path),
                        "exists": path.exists(),
                        "content": (
                            path.read_text() if args.print_content and path.exists() else None
                        ),
                    }
                print_json(payload)
                return
            for kind in kinds:
                path = paths[kind]
                print(f"[{kind}] {path}")
                if args.print_content:
                    print(path.read_text() if path.exists() else "<missing>")
            return

        if args.command == "tail":
            paths = service.loop_paths(args.loop_id, iteration=args.iteration)
            print(_read_log_excerpt(paths[args.kind], args.lines))
            return

        if args.command == "remove":
            service.remove_loop(args.loop_id, force=args.force)
            print(f"Removed loop: {args.loop_id}")
            return
    finally:
        set_color_mode(previous_color_mode)


if __name__ == "__main__":
    main()
