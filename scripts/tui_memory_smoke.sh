#!/usr/bin/env bash

set -euo pipefail

sandbox_root="${1:-$(mktemp -d /tmp/ailoop-tui-smoke.XXXXXX)}"
config_path="$sandbox_root/config.yaml"
state_dir="$sandbox_root/state"
session_name="${AILOOP_TUI_SMOKE_SESSION:-ailoop-tui-smoke}"

mkdir -p "$state_dir"

cat > "$config_path" <<EOF
default_runner: test
default_agent: orchestrator
paths:
  agent_file: null
  state_dir: $state_dir
prompt:
  pre_prompt_enabled: false
  attach_agent_file: false
  pre_prompt: ""
loop:
  steps: null
  pause_seconds: 0
  continue_on_error: true
  retry_count: 0
tasks:
  file: null
  stop_when_complete: false
  max_doing: 1
runners:
  test:
    command: python3
    args: ["-c", "print('ok')"]
    env: {}
EOF

uv run python -m ailoop.cli --config "$config_path" memory save "Preset One" "Review presets" --runner test --favorite >/dev/null
uv run python -m ailoop.cli --config "$config_path" memory save "History One" "Review history" --runner test --kind history >/dev/null
uv run python -m ailoop.cli --config "$config_path" memory save "Preset Two" "Another preset" --runner test --label ops >/dev/null

tmux kill-session -t "$session_name" 2>/dev/null || true
tmux new-session -d -s "$session_name" \
  "cd $(pwd) && CI=true uv run python -m ailoop.cli --config '$config_path' tui"

echo "sandbox: $sandbox_root"
echo "config: $config_path"
echo "session: $session_name"
echo "next: tmux attach -t $session_name"
