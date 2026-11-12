"""
Cover Letter 生成模块 — 根据简历 + JD 自动撰写 Cover Letter。
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("linkedin_autoapply.cover_letter")

COVER_LETTER_SYSTEM_PROMPT = """You are a professional cover letter writer. You write compelling, tailored cover letters that highlight the candidate's most relevant qualifications for a specific job.

## Guidelines:
1. Keep it concise — 250-350 words
2. Address the hiring manager by name if mentioned in the JD, otherwise use "Dear Hiring Manager,"
3. Opening: Express genuine interest in the role and company
4. Body 1: Link the candidate's most relevant experience/skills to the JD's key requirements
5. Body 2: Highlight a specific project or achievement that demonstrates fit
6. Closing: Reiterate interest, mention availability, and include contact information
7. Use a professional but warm tone — not too formal, not too casual
8. NEVER fabricate experiences not in the resume
9. Focus on 2-3 most relevant qualifications rather than listing everything

## Output Format:
Output the cover letter as plain text, starting with the date line. Do not include any meta-commentary.
"""


def generate_cover_letter(
    resume_text: str,
    jd_text: str,
    job_info: dict,
    match_result: dict,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> Optional[str]:
    """生成针对特定岗位的 Cover Letter。

    Args:
        resume_text: 简历全文
        jd_text: JD 全文
        job_info: 岗位元信息（含 company, title 等）
        match_result: 匹配结果（含 strengths, gaps 等）
        api_key: Anthropic API key
        model: Claude 模型

    Returns:
        Cover Letter 文本，或 None
    """
    company = job_info.get("company", "the company")
    title = job_info.get("title", "this position")
    strengths = match_result.get("strengths", [])
    gaps = match_result.get("gaps", [])
    match_score = match_result.get("overall_score", "N/A")

    strengths_text = "\n".join(f"- {s}" for s in strengths) if strengths else "N/A"
    gaps_text = "\n".join(f"- {g}" for g in gaps) if gaps else "None identified"

    prompt = f"""Write a tailored cover letter for the following candidate and job.

## Candidate Resume
{resume_text[:3000]}

## Job Description
**Company:** {company}
**Title:** {title}
**Location:** {job_info.get('location', 'Unknown')}

{jd_text[:3000]}

## Key Strengths (from AI match analysis, score: {match_score}/10):
{strengths_text}

## Potential Gaps:
{gaps_text}

---

Please write the cover letter now. Start with the date line ({datetime.now().strftime('%B %d, %Y')})."""

    try:
        from anthropic import Anthropic
    except ImportError:
        logger.error("Anthropic SDK is not installed; use the configured AI supervisor for cover letters")
        return None

    client = Anthropic(api_key=api_key)

    try:
        logger.info(f"Generating cover letter for {title} at {company}...")
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=COVER_LETTER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )

        content = response.content[0].text if response.content else ""
        if content:
            logger.info(f"Cover letter generated: {len(content)} characters")
            return content.strip()
        else:
            logger.warning("Empty response from Claude when generating cover letter")
            return None

    except Exception as e:
        logger.error(f"Cover letter generation failed: {e}")
        return None


def save_cover_letter(
    text: str,
    job_title: str,
    company: str,
    cover_letter_dir: Path,
) -> Optional[str]:
    """保存 Cover Letter 到文件。

    Args:
        text: Cover Letter 内容
        job_title: 岗位名称
        company: 公司名称
        cover_letter_dir: 存放目录

    Returns:
        保存的文件路径，或 None
    """
    # 清理文件名中的非法字符
    safe_title = re.sub(r'[<>:"/\\|?*\n\r]', '', job_title).strip()
    safe_company = re.sub(r'[<>:"/\\|?*\n\r]', '', company).strip()

    # 限制长度
    if len(safe_title) > 50:
        safe_title = safe_title[:50]
    if len(safe_company) > 30:
        safe_company = safe_company[:30]

    filename = f"cover_letter_{safe_title}_{safe_company}.md"
    filepath = cover_letter_dir / filename

    # 如果文件已存在，加序号
    counter = 1
    while filepath.exists():
        filename = f"cover_letter_{safe_title}_{safe_company}_{counter}.md"
        filepath = cover_letter_dir / filename
        counter += 1

    # 构建带 frontmatter 的 Markdown 文件
    content = f"""---
job_title: "{job_title}"
company: "{company}"
date: "{datetime.now().strftime('%Y-%m-%d')}"
---

{text}
"""
    cover_letter_dir.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info(f"Cover letter saved: {filepath}")
    return str(filepath)


def detect_cover_letter_needed(jd_text: str) -> bool:
    """检测 JD 中是否要求 Cover Letter。

    Args:
        jd_text: JD 文本

    Returns:
        True 如果 JD 提到需要 Cover Letter
    """
    keywords = [
        "cover letter",
        "covering letter",
        "letter of motivation",
        "motivation letter",
        "personal statement",
    ]
    jd_lower = jd_text.lower()
    for kw in keywords:
        if kw in jd_lower:
            return True
    return False
