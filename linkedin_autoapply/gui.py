"""Small bilingual desktop control panel for source and packaged execution."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .codex_models import discover_codex_models


TRANSLATIONS = {
    "en": {
        "title": "LinkedIn AutoApply",
        "language": "Language",
        "keywords": "Search keywords (comma-separated)",
        "location": "Location",
        "date_posted": "Posted within",
        "max_pages": "Max pages (blank = all)",
        "threshold": "AI score threshold (out of 10)",
        "keyword_count": "Target keyword count",
        "expand_keywords": "Let AI expand and optimize keywords",
        "search_settings": "Search and scoring",
        "files": "Application files",
        "resume": "Resume *",
        "requirements": "Job requirements (optional)",
        "cover_template": "Cover Letter template (optional)",
        "additional": "Additional information / answers for AI (optional)",
        "additional_hint": "The resume is the primary source. Add answers here only when a form asks for something not in the resume. You can update this while a run is active.",
        "applications": "Application modes",
        "easy_apply": "Easy Apply",
        "external_apply": "External Apply (experimental)",
        "dry_run": "Preview only; do not submit",
        "save": "Save configuration",
        "save_notes": "Save additional information",
        "start": "Start",
        "stop": "Stop",
        "doctor": "Check environment",
        "console": "Console",
        "model_settings": "⚙ Model Configuration",
        "model_backend": "AI backend",
        "backend_codex": "Local Codex CLI",
        "backend_api": "OpenAI-compatible API",
        "codex_model": "Codex model",
        "refresh_models": "Refresh Codex models",
        "model_source": "Models are read from the local Codex catalog/config. The app never sends credentials to discover them.",
        "api_base_url": "API base URL",
        "api_model": "API model",
        "api_key_env": "API key environment variable",
        "api_json_mode": "Request JSON response mode",
        "model_saved": "Model configuration saved",
        "log": "Live run log",
        "status_idle": "Idle",
        "status_running": "Running",
        "status_saved": "Saved",
        "status_stopped": "Stop requested",
        "status_ended": "Ended (exit={code})",
        "saved_path": "Saved: {path}",
        "resume_required": "Please select a valid resume file. It is the only required file.",
        "save_failed": "Save failed",
        "rubric_tooltip": "AI rubric: (1) skills and responsibilities match, (2) alignment with the user's career direction and growth plan, (3) practical fit such as language, location, work authorization, seniority, and other stated constraints. The AI must cite JD evidence and must not invent candidate facts.",
        "check_started": "Checking environment...",
        "check_failed": "Environment check failed: {error}",
    },
    "zh": {
        "title": "LinkedIn 自动投递",
        "language": "语言",
        "keywords": "搜索关键词（逗号分隔）",
        "location": "地区",
        "date_posted": "发布时间",
        "max_pages": "最大页数（留空=全部）",
        "threshold": "AI 评分阈值（总分为10）",
        "keyword_count": "目标关键词数量",
        "expand_keywords": "允许 AI 自主扩展和优化关键词",
        "search_settings": "搜索与评分",
        "files": "投递文件",
        "resume": "简历 *",
        "requirements": "求职需求（可选）",
        "cover_template": "求职信模板（可选）",
        "additional": "给 AI 的补充信息/问题答案（可选）",
        "additional_hint": "简历是主要信息来源。只有当表单询问简历中没有的信息时，再在这里补充；运行过程中也可以更新。",
        "applications": "投递模式",
        "easy_apply": "Easy Apply",
        "external_apply": "External Apply（实验）",
        "dry_run": "仅预览，不提交",
        "save": "保存配置",
        "save_notes": "保存补充信息",
        "start": "开始运行",
        "stop": "停止运行",
        "doctor": "检查环境",
        "console": "控制台",
        "model_settings": "⚙ 模型配置",
        "model_backend": "AI 后端",
        "backend_codex": "本地 Codex CLI",
        "backend_api": "OpenAI 兼容 API",
        "codex_model": "Codex 模型",
        "refresh_models": "刷新 Codex 模型列表",
        "model_source": "模型列表读取自本地 Codex catalog/config。应用不会发送凭据来获取模型列表。",
        "api_base_url": "API 基础地址",
        "api_model": "API 模型",
        "api_key_env": "API Key 环境变量名",
        "api_json_mode": "请求 JSON 响应模式",
        "model_saved": "模型配置已保存",
        "log": "实时运行日志",
        "status_idle": "未运行",
        "status_running": "运行中",
        "status_saved": "已保存",
        "status_stopped": "已请求停止",
        "status_ended": "已结束（exit={code}）",
        "saved_path": "已保存：{path}",
        "resume_required": "请选择有效的简历文件。简历是唯一必填文件。",
        "save_failed": "保存失败",
        "rubric_tooltip": "AI 评分 rubric：1）技能与岗位职责匹配度；2）与用户职业方向和发展规划的匹配度；3）语言、地区、工作许可、岗位级别及其他已声明现实条件的匹配度。AI 必须引用 JD 证据，不能编造候选人信息。",
        "check_started": "正在检查环境……",
        "check_failed": "环境检查失败：{error}",
    },
}


DATE_POSTED_VALUES = ("Past Month", "Past 2 weeks", "Past Week", "Past 24 hours")
DATE_POSTED_LABELS = {
    "en": {
        "Past Month": "Past month",
        "Past 2 weeks": "Past 2 weeks",
        "Past Week": "Past week",
        "Past 24 hours": "Past 24 hours",
    },
    "zh": {
        "Past Month": "一个月内",
        "Past 2 weeks": "两周内",
        "Past Week": "一周内",
        "Past 24 hours": "24 小时内",
    },
}


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = project_root()
CONFIG_PATH = ROOT / "config.yaml"


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        example = ROOT / "config.example.yaml"
        if example.exists():
            CONFIG_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    if yaml is None or not CONFIG_PATH.exists():
        return {}
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _save_config(data: dict[str, Any]) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed")
    CONFIG_PATH.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _copy_user_file(source: str, label: str) -> str:
    if not source:
        return ""
    source_path = Path(source)
    if not source_path.is_absolute() and not source_path.is_file():
        source_path = ROOT / source_path
    if not source_path.is_file():
        raise FileNotFoundError(f"{label} not found: {source}")
    destination_dir = ROOT / "user_files"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source_path.name
    if source_path.resolve() != destination.resolve():
        shutil.copy2(source_path, destination)
    return str(destination.relative_to(ROOT)).replace("\\", "/")


def _display_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return str(candidate) if candidate.is_file() else ""


class AutoApplyWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.geometry("1180x900")
        self.root.minsize(980, 720)
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.text_bindings: list[tuple[tk.StringVar, str]] = []
        config = _load_config()
        search = config.get("search") or {}
        expansion = search.get("keyword_expansion") or {}
        apply = config.get("auto_apply") or {}
        matching = config.get("matching") or {}
        resume = config.get("resume") or {}
        requirements = config.get("user_requirements") or {}
        cover = config.get("cover_letter") or {}
        profile = config.get("profile") or {}
        ui = config.get("ui") or {}
        ai = config.get("ai") or {}

        self.language = tk.StringVar(value=str(ui.get("language") or "en").lower() if str(ui.get("language") or "en").lower() in TRANSLATIONS else "en")
        self.language_display = tk.StringVar(value="English" if self.language.get() == "en" else "中文")
        self.keywords = tk.StringVar(value=", ".join(search.get("keywords") or []))
        self.location = tk.StringVar(value=str(search.get("location") or ""))
        configured_date = str(search.get("date_posted") or "").strip()
        self.date_posted = tk.StringVar(value=configured_date if configured_date in DATE_POSTED_VALUES else "Past 24 hours")
        self.date_posted_display = tk.StringVar()
        raw_pages = int(search.get("max_pages") or 0)
        self.max_pages = tk.StringVar(value="" if raw_pages <= 0 else str(raw_pages))
        self.threshold = tk.StringVar(value=str(matching.get("threshold") or 6.5))
        self.keyword_count = tk.StringVar(value=str(expansion.get("target_count") or max(1, len(search.get("keywords") or []))))
        self.resume = tk.StringVar(value=_display_path(resume.get("path")))
        self.requirements = tk.StringVar(value=_display_path(requirements.get("path")))
        self.cover_template = tk.StringVar(value=_display_path(cover.get("template_path")))
        self.additional_info = str(profile.get("notes") or "")
        self.easy_apply = tk.BooleanVar(value=bool(apply.get("easy_apply", True)))
        self.external_apply = tk.BooleanVar(value=bool(apply.get("external_apply", True)))
        self.expand_keywords = tk.BooleanVar(value=bool(expansion.get("enabled", False)))
        self.dry_run = tk.BooleanVar(value=False)
        backend = str(ai.get("backend") or "codex").strip().lower()
        self.ai_backend = tk.StringVar(value="api" if backend in {"api", "openai", "openai_compatible"} else "codex")
        self.codex_models = discover_codex_models()
        self.ai_model = tk.StringVar(value=str(ai.get("model") or ""))
        self.api_model = tk.StringVar(value=str(ai.get("model") or ""))
        self.api_base_url = tk.StringVar(value=str(ai.get("base_url") or ""))
        self.api_key_env = tk.StringVar(value=str(ai.get("api_key_env") or "OPENAI_API_KEY"))
        self.api_json_mode = tk.BooleanVar(value=bool(ai.get("json_mode", False)))
        self._build()
        self._apply_language()
        self._poll_output()

    def t(self, key: str) -> str:
        return TRANSLATIONS[self.language.get()].get(key, key)

    def _bind_text(self, widget: tk.Widget, key: str) -> None:
        variable = tk.StringVar(value=self.t(key))
        self.text_bindings.append((variable, key))
        widget.configure(textvariable=variable)

    def _label(self, parent, key: str, **kwargs):
        widget = ttk.Label(parent, **kwargs)
        self._bind_text(widget, key)
        return widget

    def _button(self, parent, key: str, command, **kwargs):
        widget = ttk.Button(parent, command=command, **kwargs)
        self._bind_text(widget, key)
        return widget

    def _checkbutton(self, parent, key: str, variable):
        widget = ttk.Checkbutton(parent, variable=variable)
        self._bind_text(widget, key)
        return widget

    def _row(self, parent, key: str, variable: tk.Variable, browse: bool = False, filetypes=None):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", padx=14, pady=4)
        self._label(frame, key, width=28).pack(side="left")
        ttk.Entry(frame, textvariable=variable).pack(side="left", fill="x", expand=True)
        if browse:
            ttk.Button(frame, text="…", width=4, command=lambda: self._choose(variable, filetypes)).pack(side="left", padx=6)
        return frame

    def _section(self, parent, key: str):
        ttk.Separator(parent).pack(fill="x", padx=14, pady=8)
        self._label(parent, key).pack(anchor="w", padx=14, pady=(0, 3))

    def _build(self):
        self.root.title(self.t("title"))
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=14, pady=(10, 2))
        ttk.Label(top, text="English / 中文").pack(side="right")
        language_box = ttk.Combobox(top, textvariable=self.language_display, values=["English", "中文"], state="readonly", width=10)
        language_box.pack(side="right", padx=8)
        language_box.bind("<<ComboboxSelected>>", self._on_language_selected)

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True)
        sidebar = ttk.Frame(body, width=210)
        sidebar.pack(side="left", fill="y", padx=(10, 4), pady=10)
        sidebar.pack_propagate(False)
        self._button(sidebar, "console", lambda: self._show_page("main")).pack(fill="x", pady=4)
        self._button(sidebar, "model_settings", lambda: self._show_page("model")).pack(fill="x", pady=4)

        self.page_container = ttk.Frame(body)
        self.page_container.pack(side="left", fill="both", expand=True, padx=(4, 14), pady=10)
        main = ttk.Frame(self.page_container)
        main.pack(fill="both", expand=True)
        self.main_page = main
        self._section(main, "search_settings")
        self._row(main, "keywords", self.keywords)
        self._row(main, "location", self.location)
        date_row = ttk.Frame(main)
        date_row.pack(fill="x", padx=14, pady=4)
        self._label(date_row, "date_posted", width=28).pack(side="left")
        self.date_posted_box = ttk.Combobox(date_row, textvariable=self.date_posted_display, state="readonly")
        self.date_posted_box.pack(side="left", fill="x", expand=True)
        self.date_posted_box.bind("<<ComboboxSelected>>", self._on_date_posted_selected)
        self._refresh_date_posted_options()
        self._row(main, "max_pages", self.max_pages)

        keyword_options = ttk.Frame(main)
        keyword_options.pack(fill="x", padx=14, pady=2)
        self._checkbutton(keyword_options, "expand_keywords", self.expand_keywords).pack(side="left")
        self._label(keyword_options, "keyword_count", width=22).pack(side="left", padx=(24, 4))
        ttk.Entry(keyword_options, textvariable=self.keyword_count, width=8).pack(side="left")

        score_row = ttk.Frame(main)
        score_row.pack(fill="x", padx=14, pady=4)
        self._label(score_row, "threshold", width=28).pack(side="left")
        ttk.Entry(score_row, textvariable=self.threshold, width=12).pack(side="left")
        info = ttk.Label(score_row, text="ⓘ", cursor="question_arrow")
        info.pack(side="left", padx=8)
        info.bind("<Enter>", lambda event: self._show_rubric(event.widget))
        info.bind("<Leave>", lambda _event: self._hide_rubric())

        self._section(main, "files")
        self._row(main, "resume", self.resume, True, [("Documents", "*.pdf *.docx *.txt"), ("All files", "*.*")])
        self._row(main, "requirements", self.requirements, True, [("Text", "*.md *.txt *.yaml *.yml"), ("All files", "*.*")])
        self._row(main, "cover_template", self.cover_template, True, [("Documents", "*.docx *.md *.txt"), ("All files", "*.*")])

        self._section(main, "additional")
        self._label(main, "additional_hint").pack(anchor="w", padx=14, pady=(0, 4))
        self.notes = tk.Text(main, height=5, wrap="word")
        self.notes.pack(fill="x", padx=14, pady=(0, 4))
        self.notes.insert("1.0", self.additional_info)
        self._button(main, "save_notes", self.save).pack(anchor="e", padx=14, pady=(0, 4))

        self._section(main, "applications")
        toggles = ttk.Frame(main)
        toggles.pack(fill="x", padx=14, pady=4)
        self._checkbutton(toggles, "easy_apply", self.easy_apply).pack(side="left")
        self._checkbutton(toggles, "external_apply", self.external_apply).pack(side="left", padx=22)
        self._checkbutton(toggles, "dry_run", self.dry_run).pack(side="left", padx=22)

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", padx=14, pady=8)
        self._button(buttons, "save", self.save).pack(side="left")
        self._button(buttons, "start", self.start).pack(side="left", padx=8)
        self._button(buttons, "stop", self.stop).pack(side="left")
        self._button(buttons, "doctor", self.doctor).pack(side="right")

        self.status = tk.StringVar(value=self.t("status_idle"))
        ttk.Label(main, textvariable=self.status).pack(anchor="w", padx=14)
        self._label(main, "log").pack(anchor="w", padx=14, pady=(5, 0))
        log_frame = ttk.Frame(main)
        log_frame.pack(fill="both", expand=True, padx=14, pady=5)
        self.output = tk.Text(log_frame, height=24, state="disabled", wrap="none", font=("Consolas", 10))
        self.output.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.output.yview)
        scrollbar.pack(side="right", fill="y")
        self.output.configure(yscrollcommand=scrollbar.set)

        self.model_page = ttk.Frame(self.page_container)
        self._build_model_page()
        self._show_page("main")

    def _build_model_page(self):
        page = self.model_page
        self._section(page, "model_settings")
        backend_row = ttk.Frame(page)
        backend_row.pack(fill="x", padx=14, pady=4)
        self._label(backend_row, "model_backend", width=28).pack(side="left")
        self.backend_box = ttk.Combobox(
            backend_row,
            textvariable=self.ai_backend,
            state="readonly",
            values=[self.t("backend_codex"), self.t("backend_api")],
        )
        self.backend_box.pack(side="left", fill="x", expand=True)
        self.backend_box.bind("<<ComboboxSelected>>", lambda _event: self._on_backend_changed())

        codex_row = ttk.Frame(page)
        codex_row.pack(fill="x", padx=14, pady=4)
        self.codex_model_frame = codex_row
        self._label(codex_row, "codex_model", width=28).pack(side="left")
        self.codex_model_box = ttk.Combobox(codex_row, textvariable=self.ai_model, values=self.codex_models, state="normal")
        self.codex_model_box.pack(side="left", fill="x", expand=True)
        self._button(codex_row, "refresh_models", self._refresh_codex_models).pack(side="left", padx=6)
        self.model_source_label = self._label(page, "model_source")
        self.model_source_label.pack(anchor="w", padx=14, pady=(0, 8))

        self.api_settings_frame = ttk.Frame(page)
        self.api_settings_frame.pack(fill="x")
        self._row(self.api_settings_frame, "api_base_url", self.api_base_url)
        self._row(self.api_settings_frame, "api_model", self.api_model)
        self._row(self.api_settings_frame, "api_key_env", self.api_key_env)
        self._checkbutton(self.api_settings_frame, "api_json_mode", self.api_json_mode).pack(anchor="w", padx=14, pady=4)
        self.model_save_button = self._button(page, "save", self.save)
        self.model_save_button.pack(anchor="w", padx=14, pady=10)
        self._set_api_visibility()

    def _show_page(self, name: str):
        for page_name, page in (("main", getattr(self, "main_page", None)), ("model", getattr(self, "model_page", None))):
            if page is None:
                continue
            page.pack_forget()
            if page_name == name:
                page.pack(fill="both", expand=True)

    def _on_backend_changed(self):
        selected = self.backend_box.get()
        if selected == self.t("backend_api"):
            self.ai_backend.set("api")
        else:
            self.ai_backend.set("codex")
        self._set_api_visibility()

    def _set_api_visibility(self):
        api_frame = getattr(self, "api_settings_frame", None)
        codex_frame = getattr(self, "codex_model_frame", None)
        if api_frame is None or codex_frame is None:
            return
        api_frame.pack_forget()
        codex_frame.pack_forget()
        if self.ai_backend.get() == "api":
            api_frame.pack(fill="x", before=getattr(self, "model_save_button", None))
        else:
            codex_frame.pack(fill="x", before=getattr(self, "model_source_label", None))

    def _refresh_codex_models(self):
        self.codex_models = discover_codex_models()
        self.codex_model_box.configure(values=self.codex_models)
        if self.codex_models and not self.ai_model.get().strip():
            self.ai_model.set(self.codex_models[0])
        self.status.set(self.t("model_saved"))

    def _apply_language(self):
        self.root.title(self.t("title"))
        for variable, key in self.text_bindings:
            variable.set(self.t(key))
        if hasattr(self, "backend_box"):
            self.backend_box.configure(values=[self.t("backend_codex"), self.t("backend_api")])
            self.backend_box.set(self.t("backend_api") if self.ai_backend.get() == "api" else self.t("backend_codex"))
            self._set_api_visibility()
        if hasattr(self, "date_posted_box"):
            self._refresh_date_posted_options()
        if hasattr(self, "status") and not self.process:
            self.status.set(self.t("status_idle"))

    def _on_language_selected(self, _event=None):
        self.language.set("zh" if self.language_display.get() == "中文" else "en")
        self._apply_language()

    def _refresh_date_posted_options(self):
        labels = [DATE_POSTED_LABELS[self.language.get()][value] for value in DATE_POSTED_VALUES]
        self.date_posted_box.configure(values=labels)
        current = self.date_posted.get()
        if current not in DATE_POSTED_VALUES:
            current = "Past 24 hours"
            self.date_posted.set(current)
        self.date_posted_display.set(DATE_POSTED_LABELS[self.language.get()][current])

    def _on_date_posted_selected(self, _event=None):
        selected = self.date_posted_display.get()
        labels = DATE_POSTED_LABELS[self.language.get()]
        self.date_posted.set(next((value for value, label in labels.items() if label == selected), "Past 24 hours"))

    def _show_rubric(self, anchor):
        self._hide_rubric()
        self.rubric_popup = tk.Toplevel(self.root)
        self.rubric_popup.wm_overrideredirect(True)
        self.rubric_popup.attributes("-topmost", True)
        label = tk.Label(self.rubric_popup, text=self.t("rubric_tooltip"), justify="left", wraplength=420, bg="#fffde6", relief="solid", borderwidth=1, padx=8, pady=8)
        label.pack()
        self.rubric_popup.geometry(f"+{anchor.winfo_rootx()+18}+{anchor.winfo_rooty()+18}")

    def _hide_rubric(self):
        popup = getattr(self, "rubric_popup", None)
        if popup is not None:
            popup.destroy()
            self.rubric_popup = None

    def _choose(self, variable: tk.Variable, filetypes):
        value = filedialog.askopenfilename(filetypes=filetypes or [("All files", "*.*")])
        if value:
            variable.set(value)

    def save(self):
        try:
            data = _load_config()
            resume_source = self.resume.get().strip()
            resume_path = _copy_user_file(resume_source, "Resume") if resume_source else ""
            if not resume_path:
                raise ValueError(self.t("resume_required"))

            search = data.setdefault("search", {})
            search["keywords"] = [x.strip() for x in self.keywords.get().split(",") if x.strip()]
            search["location"] = self.location.get().strip()
            search["date_posted"] = self.date_posted.get().strip()
            search["max_pages"] = int(self.max_pages.get().strip() or 0)
            expansion = search.setdefault("keyword_expansion", {})
            expansion["enabled"] = bool(self.expand_keywords.get())
            expansion["target_count"] = max(1, int(self.keyword_count.get().strip() or len(search["keywords"])))
            feedback = search.setdefault("keyword_feedback", {})
            feedback["enabled"] = bool(self.expand_keywords.get())

            data.setdefault("matching", {})["threshold"] = float(self.threshold.get().strip() or 6.5)
            data.setdefault("auto_apply", {})["easy_apply"] = bool(self.easy_apply.get())
            data.setdefault("auto_apply", {})["external_apply"] = bool(self.external_apply.get())
            data.setdefault("resume", {})["path"] = resume_path
            data.setdefault("user_requirements", {})["path"] = _copy_user_file(self.requirements.get().strip(), "Requirements") if self.requirements.get().strip() else ""
            data.setdefault("cover_letter", {})["template_path"] = _copy_user_file(self.cover_template.get().strip(), "Cover Letter template") if self.cover_template.get().strip() else ""
            data.setdefault("profile", {})["notes"] = self.notes.get("1.0", "end").strip()
            data.setdefault("ui", {})["language"] = self.language.get()
            ai = data.setdefault("ai", {})
            ai["backend"] = "api" if self.ai_backend.get() == "api" else "codex"
            ai["model"] = self.api_model.get().strip() if ai["backend"] == "api" else self.ai_model.get().strip()
            ai["base_url"] = self.api_base_url.get().strip()
            ai["api_key_env"] = self.api_key_env.get().strip() or "OPENAI_API_KEY"
            ai["json_mode"] = bool(self.api_json_mode.get())
            _save_config(data)
            self.status.set(self.t("saved_path").format(path=CONFIG_PATH))
            return True
        except Exception as exc:
            messagebox.showerror(self.t("save_failed"), str(exc))
            return False

    def _append(self, text: str):
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def start(self):
        if self.process and self.process.poll() is None:
            return
        if not self.save() or not CONFIG_PATH.exists():
            return
        max_pages = self.max_pages.get().strip() or "0"
        if getattr(sys, "frozen", False):
            worker = Path(sys.executable).with_name("LinkedInAutoApplyWorker.exe")
            command = [str(worker), "--config", str(CONFIG_PATH), "--max-pages", max_pages]
        else:
            command = [sys.executable, str(ROOT / "scripts" / "run_all_keywords.py"), "--config", str(CONFIG_PATH), "--max-pages", max_pages]
        if self.date_posted.get().strip():
            command.extend(["--date-posted", self.date_posted.get().strip()])
        if self.dry_run.get():
            command.append("--dry-run")
        env = os.environ.copy()
        env["LINKEDIN_AUTOAPPLY_AI_MODE"] = "codex"
        self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self.status.set(self.t("status_running"))
        threading.Thread(target=self._read_process, daemon=True).start()

    def _read_process(self):
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            self.output_queue.put(line)
        self.output_queue.put("[launcher] worker exited\n")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.status.set(self.t("status_stopped"))

    def doctor(self):
        self._append(self.t("check_started") + "\n")
        try:
            from .cli import doctor as run_doctor
            code = run_doctor(CONFIG_PATH)
            self._append(f"doctor exit={code}\n")
        except Exception as exc:
            self._append(self.t("check_failed").format(error=exc) + "\n")

    def _poll_output(self):
        try:
            while True:
                self._append(self.output_queue.get_nowait())
        except queue.Empty:
            pass
        if self.process and self.process.poll() is not None:
            self.status.set(self.t("status_ended").format(code=self.process.returncode))
        self.root.after(250, self._poll_output)


def main() -> int:
    root = tk.Tk()
    AutoApplyWindow(root)
    root.mainloop()
    return 0
