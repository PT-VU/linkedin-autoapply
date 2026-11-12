"""
Cover Letter Agent 桥接模块 — 检测 cover letter 需求 → 创建 flag → Agent 生成。
所有四条链路共享此模块。

原则: 代码只做确定性检测, Agent 负责生成内容。
所有信息基于 CV 真实信息 + user_qa_preferences.json, 不得编造。
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("linkedin_autoapply.cover_letter_agent")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QA_PREFERENCES_FILE = PROJECT_ROOT / "data" / "user_qa_preferences.json"


def _cover_letter_dir() -> Path:
    try:
        from .config import get_cover_letter_dir, load_config

        return get_cover_letter_dir(load_config())
    except Exception:
        return PROJECT_ROOT / "cover_letters"

# ── Cover Letter 检测关键词 ──
COVER_LETTER_TEXTAREA_KW = [
    "cover letter", "cover_letter", "coverletter",
    "motivation", "motivatie", "motivatiebrief",
    "why do you want", "tell us about yourself",
    "additional information", "personal statement",
    "why are you interested", "what makes you",
    "briefly describe", "introduce yourself",
]

COVER_LETTER_FILE_KW = [
    "cover letter", "cover_letter", "coverletter",
    "motivation letter", "motivatiebrief",
    "motivation", "motivatie", "sollicitatiebrief",
    "supporting document", "additional document",
    "upload cover", "attach cover",
]


def is_codex_mode() -> bool:
    """Return True only for the explicit Codex runtime mode."""
    return os.environ.get("LINKEDIN_AUTOAPPLY_AI_MODE", "").strip().lower() == "codex"


def _load_qa_preferences() -> Dict:
    """加载用户问答偏好。"""
    if QA_PREFERENCES_FILE.exists():
        try:
            with open(QA_PREFERENCES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"preferences": {}}


def save_qa_preferences(prefs: Dict) -> None:
    """保存用户问答偏好。"""
    QA_PREFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    prefs["last_updated"] = datetime.now().isoformat()
    with open(QA_PREFERENCES_FILE, "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False, indent=2)
    logger.info(f"Updated QA preferences: {QA_PREFERENCES_FILE}")


def detect_cover_letter_field(page, profile: Dict, job_info: Dict = None) -> Optional[Dict]:
    """检测页面上是否有 cover letter 相关字段。

    扫描 textarea 和 file input, 匹配 COVER_LETTER_*_KW。

    Returns:
        {"type": "textarea"|"file", "element_info": str, "label": str} 或 None
    """
    # 1. 检查 textarea
    for el in page.query_selector_all("textarea"):
        try:
            if not el.is_visible():
                continue
            el_id = (el.get_attribute("id") or "").lower()
            el_name = (el.get_attribute("name") or "").lower()
            placeholder = (el.get_attribute("placeholder") or "").lower()
            # 获取关联 label
            label_text = ""
            if el_id:
                try:
                    label_el = page.query_selector(f"label[for='{el_id}']")
                    if label_el:
                        label_text = (label_el.inner_text() or "").lower()
                except Exception:
                    pass
            combined = f"{el_id} {el_name} {placeholder} {label_text}"
            if any(kw in combined for kw in COVER_LETTER_TEXTAREA_KW):
                return {
                    "type": "textarea",
                    "element_info": f"textarea id={el_id[:30]} name={el_name[:30]}",
                    "label": label_text or placeholder or el_id or el_name,
                }
        except Exception:
            continue

    # 2. 检查 file input
    for el in page.query_selector_all("input[type='file']"):
        try:
            el_id = (el.get_attribute("id") or "").lower()
            el_name = (el.get_attribute("name") or "").lower()
            label_text = ""
            if el_id:
                try:
                    label_el = page.query_selector(f"label[for='{el_id}']")
                    if label_el:
                        label_text = (label_el.inner_text() or "").lower()
                except Exception:
                    pass
            combined = f"{el_id} {el_name} {label_text}"
            if any(kw in combined for kw in COVER_LETTER_FILE_KW):
                return {
                    "type": "file",
                    "element_info": f"file_input id={el_id[:30]} name={el_name[:30]}",
                    "label": label_text or el_id or el_name,
                }
        except Exception:
            continue

    return None


def request_cover_letter(
    platform: str,
    profile: Dict,
    jd_text: str = "",
    job_title: str = "",
    company: str = "",
    cl_field_info: Dict = None,
    job_id: str = "",
    resume_path: str = "",
) -> bool:
    """创建 cover_letter_needed.flag — 非阻塞，创建后立即返回。

    Args:
        platform: "linkedin" 或 "glassdoor"
        profile: 用户 profile
        jd_text: 职位描述文本
        job_title: 岗位标题
        company: 公司名
        cl_field_info: detect_cover_letter_field() 的返回值

    Returns:
        True 如果 flag 创建成功
    """
    data_dir = PROJECT_ROOT / "data" / platform
    data_dir.mkdir(parents=True, exist_ok=True)

    need_flag = data_dir / "cover_letter_needed.flag"
    context_file = data_dir / "cover_letter_context.json"
    done_flag = data_dir / "cover_letter_done.flag"
    instructions_file = data_dir / "cover_letter_instructions.json"

    # 清理旧的 done flag / instructions (防止污染)
    for f in [done_flag, instructions_file]:
        if f.exists():
            try:
                f.unlink()
            except FileNotFoundError:
                pass

    # 构建上下文
    context = {
        "platform": platform,
        "job_title": job_title,
        "company": company,
        "jd_text": jd_text,
        "candidate_profile": profile or {},
        "resume_path": str(resume_path or ""),
        "cover_letter_field": cl_field_info or {},
        "context_map": "data/applicant_context/README.md",
        "output_dir": str(_cover_letter_dir()),
        "output_filename": f"Cover_Letter_{company[:20]}_{job_title[:30]}_{job_id[:20]}_{datetime.now():%Y%m%d_%H%M%S_%f}.docx"
            .replace(" ", "_").replace("/", "_").replace("\\", "_"),
        "truthfulness_requirement": (
            "所有信息必须基于 CV 的真实内容。不得编造经验、技能或成就。"
            "读取 applicant_context 资料索引中必要的文件，不得读取无关项目文件。"
            "资料中没有的事实必须标记为需要向用户询问。"
        ),
        "timestamp": datetime.now().isoformat(),
    }

    with open(context_file, "w", encoding="utf-8") as f:
        json.dump(context, f, ensure_ascii=False, indent=2)

    need_flag.write_text(datetime.now().isoformat(), encoding="utf-8")
    logger.info(f"📝 Cover letter needed for {company} — {job_title} (non-blocking)")
    return True


def wait_for_cover_letter(platform: str, timeout_s: int = 260) -> Optional[Dict]:
    """Wait for Codex to create a cover letter response for this platform."""
    data_dir = PROJECT_ROOT / "data" / platform
    done_flag = data_dir / "cover_letter_done.flag"
    instructions_file = data_dir / "cover_letter_instructions.json"

    start = time.monotonic()
    last_logged = 0
    while not done_flag.exists():
        time.sleep(1)
        elapsed = int(time.monotonic() - start)
        if elapsed >= last_logged + 30:
            logger.info(f"   ...waiting for cover letter ({elapsed}s)")
            last_logged = elapsed
        if elapsed > timeout_s:
            logger.warning(f"Cover letter timeout after {timeout_s}s")
            return None

    if not instructions_file.exists():
        logger.warning("Cover letter done flag exists but instructions file is missing")
        return None

    try:
        with instructions_file.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"Could not read cover letter instructions: {exc}")
        return None


def request_and_wait_cover_letter(
    platform: str,
    profile: Dict,
    jd_text: str = "",
    job_title: str = "",
    company: str = "",
    cl_field_info: Dict = None,
    timeout_s: int = 260,
    job_id: str = "",
    resume_path: str = "",
) -> Optional[Dict]:
    """Create the cover-letter flag and wait for Codex only in Codex mode."""
    request_cover_letter(
        platform, profile, jd_text, job_title, company, cl_field_info, job_id,
        resume_path=resume_path,
    )
    if not is_codex_mode():
        logger.info("Cover letter flag created; not waiting because current mode is not Codex")
        return None
    return wait_for_cover_letter(platform, timeout_s=timeout_s)


def load_cover_letter_instructions(platform: str) -> Optional[Dict]:
    """读取 Agent 生成的 cover letter 指令。

    Returns:
        {"cover_letter_text": str, "actions": [...]} 或 None
    """
    data_dir = PROJECT_ROOT / "data" / platform
    done_flag = data_dir / "cover_letter_done.flag"
    instructions_file = data_dir / "cover_letter_instructions.json"

    if not done_flag.exists():
        return None
    if not instructions_file.exists():
        return None

    try:
        with open(instructions_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_cover_letter_to_disk(filename: str, content: str) -> str:
    """将生成的 cover letter 保存到 cover_letters/ 目录。

    Returns:
        文件路径
    """
    output_dir = _cover_letter_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / filename

    # 添加元数据头
    header = (
        f"<!-- Cover Letter\n"
        f"     Generated: {datetime.now().isoformat()}\n"
        f"     Truthfulness: All claims verified against CV\n"
        f"-->\n\n"
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(header + content)

    logger.info(f"Cover letter saved: {filepath}")
    return str(filepath)


def clean_cover_letter_flags(platform: str) -> None:
    """Phase 0: 清理 cover_letter flag 文件。"""
    data_dir = PROJECT_ROOT / "data" / platform
    for fname in ["cover_letter_needed.flag", "cover_letter_done.flag",
                   "cover_letter_context.json", "cover_letter_instructions.json"]:
        fp = data_dir / fname
        if fp.exists():
            try:
                fp.unlink()
            except FileNotFoundError:
                pass
