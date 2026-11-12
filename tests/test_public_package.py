from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_example_configuration_is_parseable_and_external_is_explicitly_experimental():
    config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert config["ai"]["backend"] == "codex"
    assert config["auto_apply"]["external_apply"] is True
    assert config["cover_letter"]["external_session_id"] == ""
    assert str(config["resume"]["path"]).startswith("user_files/")
    assert config["search"]["max_pages"] == 0
    assert config["search"]["keyword_expansion"]["enabled"] is False
    assert config["linkedin"]["auth"]["interactive_login"] is True
    assert config["linkedin"]["auth"]["login_timeout_seconds"] == 300


def test_config_defaults_are_not_user_specific(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    from src.config import get_cover_letter_dir, get_resume_path, load_config

    config = load_config(str(tmp_path / "missing.yaml"))
    assert get_resume_path(config) == ROOT / "user_files" / "resume.pdf"
    assert get_cover_letter_dir(config) == ROOT / "cover_letters"
    assert "Users" not in str(get_resume_path(config))


def test_cli_worker_command_uses_current_project_scripts(monkeypatch):
    from linkedin_autoapply.cli import _worker_command

    class Args:
        max_pages = 3
        location = "Belgium"
        date_posted = "Past 24 hours"
        start_index = 2
        no_supervisor = True
        dry_run = True

    command = _worker_command(ROOT / "config.yaml", Args())
    assert command[1].endswith("run_all_keywords.py")
    assert "--max-pages" in command and "3" in command
    assert "--location" in command and "Belgium" in command
    assert "--start-index" in command and "2" in command
    assert "--no-supervisor" in command
    assert "--dry-run" in command


def test_api_backend_parses_json_and_fenced_json():
    from linkedin_autoapply.ai_backend import parse_json_message

    assert parse_json_message('{"response_type":"scores"}') == {"response_type": "scores"}
    assert parse_json_message("```json\n{\"response_type\":\"instructions\"}\n```") == {
        "response_type": "instructions"
    }


def test_api_backend_uses_compatible_chat_completion(monkeypatch, tmp_path):
    import linkedin_autoapply.ai_backend as backend

    monkeypatch.setenv("TEST_API_KEY", "secret")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"response_type\\":\\"instructions\\",\\"event_id\\":\\"e\\"}"}}]}'

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(backend, "urlopen", fake_urlopen)
    result = backend.call_openai_compatible(
        prompt="return json",
        config={
            "base_url": "https://example.test/v1",
            "model": "test-model",
            "api_key_env": "TEST_API_KEY",
        },
        timeout=7,
        result_file=tmp_path / "result.json",
        stdout_file=tmp_path / "stdout.log",
    )
    assert result["response_type"] == "instructions"
    assert captured == {"url": "https://example.test/v1/chat/completions", "auth": "Bearer secret", "timeout": 7}
