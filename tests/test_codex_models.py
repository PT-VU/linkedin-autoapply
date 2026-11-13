from __future__ import annotations

import json


def test_codex_model_catalog_reads_local_cache(monkeypatch, tmp_path):
    from linkedin_autoapply import codex_models

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(
        json.dumps({"models": [
            {"slug": "gpt-test", "visibility": "list"},
            {"slug": "hidden", "visibility": "hidden"},
        ]}),
        encoding="utf-8",
    )
    assert codex_models.discover_codex_models() == ["gpt-test"]
