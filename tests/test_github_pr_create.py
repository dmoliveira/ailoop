from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "github_pr_create.py"


def load_module():
    spec = importlib.util.spec_from_file_location("github_pr_create", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_body_prefers_inline_text(tmp_path: Path) -> None:
    module = load_module()
    body_file = tmp_path / "body.md"
    body_file.write_text("from file")
    args = SimpleNamespace(body="inline", body_file=str(body_file))
    assert module._body(args) == "inline"


def test_main_posts_pull_request(monkeypatch, capsys) -> None:
    module = load_module()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"html_url": "https://example.test/pr/1"}).encode()

    seen = {}

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(module.subprocess, "check_output", lambda *args, **kwargs: "gh-token\n")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        "sys.argv",
        [
            "github_pr_create.py",
            "--repo",
            "owner/repo",
            "--title",
            "Title",
            "--head",
            "owner:branch",
            "--body",
            "Body text",
        ],
    )

    assert module.main() == 0
    assert seen["url"] == "https://api.github.com/repos/owner/repo/pulls"
    assert seen["timeout"] == 60
    assert seen["body"] == {
        "title": "Title",
        "head": "owner:branch",
        "base": "main",
        "body": "Body text",
    }
    assert json.loads(capsys.readouterr().out) == {"html_url": "https://example.test/pr/1"}


def test_main_prints_http_error_body(monkeypatch, capsys) -> None:
    module = load_module()

    def fake_urlopen(request, timeout):  # type: ignore[no-untyped-def]
        raise HTTPError(
            request.full_url,
            422,
            "Unprocessable Entity",
            hdrs=None,
            fp=io.BytesIO(b'{"message":"Validation Failed"}'),
        )

    monkeypatch.setattr(module.subprocess, "check_output", lambda *args, **kwargs: "gh-token\n")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        "sys.argv",
        [
            "github_pr_create.py",
            "--repo",
            "owner/repo",
            "--title",
            "Title",
            "--head",
            "owner:branch",
        ],
    )

    assert module.main() == 1
    assert '{"message":"Validation Failed"}' in capsys.readouterr().err


def test_main_prints_friendly_auth_error(monkeypatch, capsys) -> None:
    module = load_module()

    def fake_check_output(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, ["gh", "auth", "token"])

    monkeypatch.setattr(module.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(
        "sys.argv",
        [
            "github_pr_create.py",
            "--repo",
            "owner/repo",
            "--title",
            "Title",
            "--head",
            "owner:branch",
        ],
    )

    assert module.main() == 1
    assert "GitHub auth token not available" in capsys.readouterr().err
