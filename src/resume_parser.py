"""
简历解析模块 — 从 PDF / DOCX / TXT 格式简历中提取文本。
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("linkedin_autoapply.resume_parser")


def extract_text_from_pdf(pdf_path: str) -> str:
    """从 PDF 文件提取文本。

    Args:
        pdf_path: PDF 文件路径

    Returns:
        提取的文本内容
    """
    import pdfplumber

    text_parts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        logger.info(f"Extracted {len(text_parts)} pages from PDF: {pdf_path}")
        return "\n\n".join(text_parts)
    except Exception as e:
        logger.error(f"Failed to extract PDF text: {e}")
        return ""


def extract_text_from_docx(docx_path: str) -> str:
    """从 DOCX 文件提取文本。

    Args:
        docx_path: DOCX 文件路径

    Returns:
        提取的文本内容
    """
    try:
        from docx import Document

        doc = Document(docx_path)
        text_parts = [p.text for p in doc.paragraphs if p.text.strip()]
        logger.info(f"Extracted {len(text_parts)} paragraphs from DOCX: {docx_path}")
        return "\n\n".join(text_parts)
    except ImportError:
        logger.error("python-docx not installed — cannot parse DOCX")
        return ""
    except Exception as e:
        logger.error(f"Failed to extract DOCX text: {e}")
        return ""


def extract_text_from_txt(txt_path: str) -> str:
    """从纯文本文件读取简历。

    Args:
        txt_path: TXT 文件路径

    Returns:
        文本内容
    """
    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(txt_path, "r", encoding="latin-1") as f:
            return f.read()


def load_resume(path: str) -> str:
    """自动检测文件类型并加载简历文本。

    Args:
        path: 简历文件路径

    Returns:
        简历全文
    """
    filepath = Path(path)
    if not filepath.exists():
        logger.error(f"Resume file not found: {path}")
        return ""

    suffix = filepath.suffix.lower()
    logger.info(f"Loading resume: {path} (type: {suffix})")

    if suffix == ".pdf":
        return extract_text_from_pdf(str(filepath))
    elif suffix in (".docx", ".doc"):
        return extract_text_from_docx(str(filepath))
    elif suffix in (".txt", ".md"):
        return extract_text_from_txt(str(filepath))
    else:
        # 尝试按 PDF 处理
        logger.warning(f"Unknown resume format '{suffix}', trying as PDF...")
        text = extract_text_from_pdf(str(filepath))
        if not text:
            # 尝试按文本处理
            text = extract_text_from_txt(str(filepath))
        return text
