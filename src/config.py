"""
配置加载模块 — 读取 config.yaml 并提供结构化访问。
"""

import os
import yaml
from pathlib import Path
from typing import Any, Dict

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 默认配置文件路径
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# 默认配置（当 config.yaml 不存在时使用）
DEFAULTS: Dict[str, Any] = {
    "linkedin": {
        "use_saved_profile": True,
        "profile_path": "",
        "auth": {
            "interactive_login": True,
            "login_timeout_seconds": 300,
            "poll_interval_seconds": 2,
            "login_url": "https://www.linkedin.com/login",
            "success_url": "https://www.linkedin.com/feed/",
        },
    },
    "browser": {
        "browser_type": "chromium",
        "channel": "msedge",
        "headless": False,
        "slow_mo": 300,
    },
    "search": {
        "keywords": ["AI Engineer"],
        "location": "",
        "date_posted": "Past Week",
        "experience_level": [],
        "job_type": [],
        "remote": [],
        "max_pages": 0,
        "keyword_expansion": {
            "enabled": False,
            "target_count": 10,
            "candidate_keywords": [],
        },
        "keyword_feedback": {
            "enabled": True,
            "window_days": 7,
            "minimum_jobs": 15,
            "minimum_runs": 3,
            "minimum_match_rate": 0.12,
                "replace_zero_result_after_runs": 3,
            "max_replacements_per_update": 1,
            "candidate_keywords": [],
            "families": {},
        },
    },
    "resume": {
        "path": "user_files/resume.pdf",
    },
    "user_requirements": {
        "path": "user_files/job_requirements.md",
    },
    "matching": {
        "threshold": 6.5,
        "easy_apply_threshold": 5.0,
        "model": "codex",
        "rubric_path": "data/scoring_rubric.md",
    },
    "ai": {
        "backend": "codex",
        "command": "codex",
        "model": "",
        "sandbox": "workspace-write",
        "timeout_seconds": 260,
    },
    "auto_apply": {
        "easy_apply": True,
        "external_apply": True,
        # Domains that must remain manual even when external automation is on.
        "manual_external_domains": [],
        "max_applications_per_run": 20,
        "confirm_before_apply": False,
    },
    "external_agent": {
        "timeout_seconds": 600,
        "handoff_timeout_seconds": 120,
        "defer_handoff": True,
    },
    "manual_apply_queue": {
        "enabled": True,
        "path": "data/manual_apply_queue.xlsx",
    },
    "cover_letter": {
        "enabled": True,
        "folder": "cover_letters",
        "naming": "cover_letter_{job_title}_{company}.md",
        "template_path": "user_files/cover_letter_template.docx",
        "external_session_id": "",
        "external_output_dir": "cover_letters",
        "external_timeout_seconds": 300,
    },
    "logging": {
        "level": "INFO",
        "file": "logs/run.log",
        "screenshot_on": ["submit", "error"],
    },
}


def _merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge user settings without mutating the defaults."""
    result = dict(base)
    for section, value in override.items():
        if isinstance(result.get(section), dict) and isinstance(value, dict):
            result[section] = _merge_dicts(result[section], value)
        else:
            result[section] = value
    return result


def load_config(path: str = None) -> Dict[str, Any]:
    """加载 YAML 配置，合并默认值。

    Args:
        path: 配置文件路径，默认项目根目录 config.yaml

    Returns:
        合并后的配置字典
    """
    config = _merge_dicts(DEFAULTS, {})

    if path is None:
        path = str(DEFAULT_CONFIG_PATH)

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}

        config = _merge_dicts(config, user_config)

    return config


def get_resume_path(config: Dict[str, Any]) -> Path:
    """获取简历文件的绝对路径。"""
    path = config.get("resume", {}).get("path", "")
    return _resolve_path(path)


def get_user_requirements_path(config: Dict[str, Any]) -> Path:
    return _resolve_path(config.get("user_requirements", {}).get("path", ""))


def _resolve_path(path: Any) -> Path:
    resolved = Path(str(path or ""))
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    return resolved


def get_cover_letter_dir(config: Dict[str, Any]) -> Path:
    """获取 Cover Letter 目录的绝对路径。"""
    folder = config.get("cover_letter", {}).get("folder", "cover_letters")
    resolved = Path(folder)
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
