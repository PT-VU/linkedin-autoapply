"""
通用表单填写引擎 — 智能识别表单字段并自动填写。
支持：文本框、下拉选择、单选/多选、文件上传等。
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import Page, ElementHandle

logger = logging.getLogger("linkedin_autoapply.form_filler")

# 字段语义映射 — label 文本 → profile 字段
FIELD_MAPPING: Dict[str, str] = {
    # 姓名
    "first name": "first_name",
    "firstname": "first_name",
    "given name": "first_name",
    "last name": "last_name",
    "lastname": "last_name",
    "surname": "last_name",
    "family name": "last_name",
    "full name": "full_name",
    "name": "full_name",

    # 联系方式
    "email": "email",
    "e-mail": "email",
    "email address": "email",
    "phone": "phone",
    "phone number": "phone",
    "telephone": "phone",
    "mobile": "phone",
    "mobile phone": "phone",

    # 地址
    "address": "address",
    "city": "city",
    "town": "city",
    "country": "country",
    "location": "location",
    "state": "state",
    "province": "state",
    "zip": "zip",
    "zip code": "zip",
    "postal code": "zip",

    # 个人链接
    "linkedin": "linkedin_url",
    "linkedin url": "linkedin_url",
    "linkedin profile": "linkedin_url",
    "website": "website",
    "portfolio": "website",
    "github": "github",

    # 工作授权
    "legally authorized": "work_authorization",
    "work authorization": "work_authorization",
    "eligible to work": "work_authorization",
    "right to work": "work_authorization",
    "authorized to work": "work_authorization",
    "sponsorship": "sponsorship_required",
    "visa sponsorship": "sponsorship_required",
    "require sponsorship": "sponsorship_required",
    "need sponsorship": "sponsorship_required",
    "sponsorship required": "sponsorship_required",

    # 其他常见字段
    "current company": "current_company",
    "current title": "current_title",
    "current role": "current_title",
    "notice period": "notice_period",
    "salary expectation": "salary_expectation",
    "expected salary": "salary_expectation",
    "desired salary": "salary_expectation",
    "relocate": "willing_to_relocate",
    "willing to relocate": "willing_to_relocate",
    "relocation": "willing_to_relocate",

    # 简历/CV
    "resume": "resume",
    "cv": "resume",
    "résumé": "resume",
    "upload resume": "resume",
    "attach resume": "resume",
    "cover letter": "cover_letter",
    "covering letter": "cover_letter",
    "motivation letter": "cover_letter",
}

# 常见 Yes/No 问题的归一化
YES_NO_PATTERNS: Dict[str, List[str]] = {
    "work_authorization": ["yes", "i am", "authorized", "eligible", "legally"],
    "sponsorship_required": ["yes", "i do require", "will require", "need sponsorship", "require sponsorship"],
    "willing_to_relocate": ["yes", "i am", "willing"],
}


def _find_label_text(el: ElementHandle) -> str:
    """获取表单字段的 label 文本（通过多种方式）。"""
    # 方法 1：关联的 <label> 元素
    el_id = el.get_attribute("id")
    if el_id:
        label_el = el.evaluate_handle(
            f'document.querySelector(\'label[for="{el_id}"]\')'
        )
        if label_el:
            try:
                text = label_el.inner_text()
                if text:
                    return text.strip().lower()
            except Exception:
                pass

    # 方法 2：父级 <label>
    parent = el.evaluate_handle("el => el.closest('label')")
    if parent:
        try:
            text = parent.inner_text()
            if text:
                return text.strip().lower()
        except Exception:
            pass

    # 方法 3：前面的 <label> / <span>
    try:
        prev_label = el.evaluate_handle(
            'el => { const prev = el.previousElementSibling; return prev && (prev.tagName === "LABEL" || prev.tagName === "SPAN") ? prev.textContent : ""; }'
        )
        text = prev_label.json_value() if prev_label else ""
        if text:
            return text.strip().lower()
    except Exception:
        pass

    # 方法 4：placeholder
    placeholder = el.get_attribute("placeholder")
    if placeholder:
        return placeholder.strip().lower()

    # 方法 5：aria-label
    aria_label = el.get_attribute("aria-label")
    if aria_label:
        return aria_label.strip().lower()

    return ""


def _match_field(label_text: str) -> Optional[str]:
    """根据 label 文本匹配 profile 字段名。

    Args:
        label_text: 标准化后的 label 文本

    Returns:
        profile 字段名，或 None
    """
    label_lower = label_text.lower().strip()

    # 精确匹配
    if label_lower in FIELD_MAPPING:
        return FIELD_MAPPING[label_lower]

    # 模糊匹配
    for keyword, field in FIELD_MAPPING.items():
        if keyword in label_lower:
            return field

    return None


def fill_text_input(page: Page, el: ElementHandle, value: str) -> bool:
    """填写文本输入框。

    Args:
        page: Page 对象
        el: 输入框元素
        value: 要填写的值

    Returns:
        True 如果填写成功
    """
    try:
        el.click()
        el.fill("")
        el.type(value, delay=50)  # 模拟人类输入
        logger.debug(f"Filled text input with: {value[:30]}...")
        return True
    except Exception as e:
        logger.debug(f"Failed to fill text input: {e}")
        return False


def fill_select(page: Page, el: ElementHandle, value: str) -> bool:
    """填写下拉选择框。

    Args:
        page: Page 对象
        el: select 元素或下拉控件
        value: 要选择的值

    Returns:
        True 如果选择成功
    """
    try:
        tag = el.evaluate("el => el.tagName").lower()

        if tag == "select":
            # 原生 select
            el.select_option(label=value)
            return True

        # 自定义下拉：点击打开 → 搜索 → 点击选项
        el.click()
        page.wait_for_timeout(500)

        # 尝试找到匹配的选项
        option = page.query_selector(
            f'[role="option"]:has-text("{value}"), '
            f'li:has-text("{value}"), '
            f'div:has-text("{value}")'
        )
        if option:
            option.click()
            page.wait_for_timeout(300)
            return True

        # 如果没找到，尝试输入文本
        input_el = el.query_selector("input")
        if input_el:
            input_el.type(value, delay=30)
            page.wait_for_timeout(500)
            page.keyboard.press("Enter")
            return True

        return False

    except Exception as e:
        logger.debug(f"Failed to fill select: {e}")
        return False


def fill_radio_or_checkbox(el: ElementHandle, value: str = "yes") -> bool:
    """处理单选/多选按钮。

    Args:
        el: radio/checkbox 元素
        value: desired value ("yes" / "no")

    Returns:
        True 如果操作成功
    """
    try:
        el_type = el.get_attribute("type")
        if el_type == "checkbox":
            if value.lower() in ("yes", "true", "1"):
                if not el.is_checked():
                    el.check()
            else:
                if el.is_checked():
                    el.uncheck()
        else:
            el.click()

        return True
    except Exception as e:
        logger.debug(f"Failed to handle radio/checkbox: {e}")
        return False


def upload_file(el: ElementHandle, file_path: str) -> bool:
    """上传文件到 input[type=file] 控件。

    Args:
        el: file input 元素
        file_path: 要上传的文件路径

    Returns:
        True 如果上传成功
    """
    try:
        abs_path = str(Path(file_path).resolve())
        el.set_input_files(abs_path)
        logger.info(f"Uploaded file: {abs_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to upload file '{file_path}': {e}")
        return False


def auto_fill_form(
    page: Page,
    profile: Dict[str, Any],
    resume_path: str = "",
    cover_letter_path: str = "",
) -> bool:
    """自动填写当前页面上的表单。

    遍历所有输入控件，根据 label 文本匹配 profile 字段并填写。

    Args:
        page: Page 对象（应在表单页面上）
        profile: 用户的个人信息字典
        resume_path: 简历文件路径（需要上传时使用）
        cover_letter_path: Cover Letter 路径（需要上传时使用）

    Returns:
        True 如果表单成功提交
    """
    logger.info("Auto-filling form...")
    filled_count = 0

    # 1. 处理文本输入框
    text_inputs = page.query_selector_all("input[type='text'], input[type='email'], input[type='tel'], input:not([type])")
    for el in text_inputs:
        try:
            label = _find_label_text(el)
            field = _match_field(label)
            if field and field in profile:
                value = str(profile[field])
                if fill_text_input(page, el, value):
                    filled_count += 1
        except Exception:
            continue

    # 2. 处理 textarea
    textareas = page.query_selector_all("textarea")
    for el in textareas:
        try:
            label = _find_label_text(el)
            field = _match_field(label)
            if field and field in profile:
                value = str(profile[field])
                if fill_text_input(page, el, value):
                    filled_count += 1
        except Exception:
            continue

    # 3. 处理 select / 下拉
    selects = page.query_selector_all("select, [role='combobox'], [role='listbox']")
    for el in selects:
        try:
            label = _find_label_text(el)
            field = _match_field(label)
            if field and field in profile:
                value = str(profile[field])
                if fill_select(page, el, value):
                    filled_count += 1
        except Exception:
            continue

    # 4. 处理文件上传
    file_inputs = page.query_selector_all("input[type='file']")
    for el in file_inputs:
        try:
            label = _find_label_text(el)
            field = _match_field(label)
            if field == "resume" and resume_path:
                if upload_file(el, resume_path):
                    filled_count += 1
            elif field == "cover_letter" and cover_letter_path:
                if upload_file(el, cover_letter_path):
                    filled_count += 1
        except Exception:
            continue

    # 5. 处理 radio / checkbox 组
    radio_checkbox = page.query_selector_all("input[type='radio'], input[type='checkbox']")
    processed_groups = set()
    for el in radio_checkbox:
        try:
            name = el.get_attribute("name") or el.get_attribute("id") or ""
            if name in processed_groups:
                continue

            label = _find_label_text(el)
            # 如果元素的 label 没有匹配，尝试找同组的 label
            if el.get_attribute("type") == "radio":
                # 找同一 name 组的所有 radio
                group_name = el.get_attribute("name")
                if group_name:
                    processed_groups.add(group_name)
                    # 获取整个 radio group 的上下文
                    parent_text = el.evaluate_handle(
                        'el => { const p = el.closest("fieldset") || el.closest("div"); return p ? p.textContent : ""; }'
                    )
                    try:
                        group_label = parent_text.json_value() if parent_text else ""
                    except Exception:
                        group_label = ""
                    combined_label = f"{_find_label_text(el)} {group_label}".lower()
                else:
                    combined_label = label

                field = _match_field(combined_label)

                if field in YES_NO_PATTERNS:
                    desired = YES_NO_PATTERNS[field]
                    # 找匹配的 radio option
                    group_radios = page.query_selector_all(f'input[name="{group_name}"]')
                    for r in group_radios:
                        r_label = _find_label_text(r)
                        r_value = (r.get_attribute("value") or "").lower()
                        combined = f"{r_label} {r_value}".lower()
                        for d in desired:
                            if d in combined:
                                if not r.is_checked():
                                    r.click()
                                break
            else:
                # Checkbox
                field = _match_field(label)
                if field in YES_NO_PATTERNS:
                    desired = YES_NO_PATTERNS[field]
                    should_check = any(d in desired for d in ["yes", "i am", "authorized", "eligible", "willing"])
                    if el.is_checked() != should_check:
                        el.click()
                    processed_groups.add(name)

        except Exception:
            continue

    logger.info(f"Form auto-fill complete: {filled_count} fields filled")
    return filled_count > 0


def click_submit_button(page: Page) -> bool:
    """点击表单提交按钮。

    Args:
        page: Page 对象

    Returns:
        True 如果点击成功
    """
    submit_selectors = [
        'button[type="submit"]',
        'button:has-text("Submit")',
        'button:has-text("Submit application")',
        'button:has-text("Review")',
        'button:has-text("Next")',
        'button:has-text("Continue")',
        'button:has-text("Apply")',
        'button:has-text("Send")',
        'button:has-text("Done")',
        'button:has-text("Save")',
        'input[type="submit"]',
        '[aria-label="Submit"]',
        '[aria-label="Submit application"]',
    ]

    for selector in submit_selectors:
        btn = page.query_selector(selector)
        if btn and btn.is_visible():
            if not btn.is_disabled():
                btn.click()
                logger.info(f"Clicked: {selector}")
                return True

    logger.warning("No submit button found")
    return False
