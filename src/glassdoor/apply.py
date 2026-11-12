"""
Glassdoor 投递模块 — Snelsollicitatie (Glassdoor原生 + Indeed Apply) + External Apply。
"""
import logging, time
from typing import Dict, Any
from playwright.sync_api import Page

logger = logging.getLogger("glassdoor")


def handle_quick_apply_flow(
    page: Page, profile: Dict[str, Any],
    resume_path: str, max_steps: int = 8,
) -> bool:
    """Glassdoor Snelsollicitatie 流程（原生表单 / Indeed Apply 自适应）。"""
    # 1. 点击 Snelsollicitatie
    clicked = False
    for btn in page.query_selector_all("button, a"):
        try:
            if not btn.is_visible(): continue
            txt = (btn.inner_text() or "").strip().lower()
            if "snelsollicitatie" in txt:
                btn.click(force=True)
                page.wait_for_timeout(4000)
                clicked = True
                logger.info("Clicked Snelsollicitatie")
                break
        except Exception: continue

    if not clicked:
        logger.warning("No Snelsollicitatie button")
        return False

    # 2. 检测 Indeed Apply (新标签页或页面跳转)
    indeed_page = _find_indeed_page(page)
    if indeed_page:
        logger.info(f"Indeed Apply detected: {indeed_page.url[:100]}")
        return _fill_indeed_apply(indeed_page, profile, resume_path, max_steps)

    # 3. 原生 Glassdoor 表单
    return _fill_native_glassdoor_form(page, profile, resume_path, max_steps)


# ── Indeed Apply 表单填写 ──

def _find_indeed_page(page: Page):
    """查找 Indeed Apply 页面（新标签页或页面跳转，不是 iframe）。"""
    # 检查当前页面是否已跳转到 Indeed
    if "indeed" in (page.url or "").lower():
        return page
    # 检查是否有新标签页打开了 Indeed
    for p in page.context.pages:
        try:
            if "indeed" in (p.url or "").lower() or "smartapply" in (p.url or "").lower():
                p.bring_to_front()
                return p
        except Exception: pass
    return None


def _fill_indeed_apply(page: Page, profile: Dict, resume_path: str, max_steps: int) -> bool:
    """填写 Indeed Apply 表单。卡住时触发 AI HELP FLAG。"""
    # ── P0修复: 关闭所有旧 Indeed 页面, 强制从 contact-info 开始 ──
    for p in list(page.context.pages):
        try:
            if "indeed" in (p.url or "").lower() and p != page:
                logger.info(f"Closing stale Indeed page: {p.url[:80]}")
                p.close()
        except Exception:
            pass

    logger.info(f"Indeed Apply START: {page.url[:150]}")
    _log_page_state(page, "INDEED_START")

    # P1修复: 追踪同一 URL 步骤, 防止 screenings questions 无限循环
    prev_url = ""
    same_url_count = 0

    for step in range(1, max_steps + 1):
        page.wait_for_timeout(2000)

        if _check_success_text(page):
            logger.info("Indeed Apply: SUBMITTED!")
            return True

        current_url = page.url.split('?')[0].split('/')[-1]  # 去掉 query string
        logger.info(f"Indeed step {step}/{max_steps} — URL: {current_url}")
        _log_page_state(page, f"INDEED_STEP_{step}")

        # P1修复: 同一 URL 连续 3 步 → 放弃 (screenings 循环)
        if current_url == prev_url:
            same_url_count += 1
            if same_url_count >= 3:
                logger.warning(f"Same URL '{current_url}' for {same_url_count} steps — triggering AI HELP")
                _request_help(page, profile, resume_path)
                if _wait_for_ai_help(page):
                    logger.info("Indeed: AI help received after screenings loop, retrying...")
                    same_url_count = 0
                    prev_url = ""
                    continue
                logger.warning("Indeed: AI help timeout after screenings loop")
                break
        else:
            same_url_count = 0
        prev_url = current_url

        # 填表
        filled = _fill_form_fields(page, profile, resume_path)
        logger.info(f"Indeed step {step}: filled {filled} fields")

        # 推进
        if not _click_next_or_submit(page):
            if filled == 0:
                # review-module: 尝试提交（即使检测到 reCAPTCHA 文本，按钮可能仍可点击）
                if 'review-module' in (page.url or '').lower():
                    body = page.evaluate('() => document.body ? document.body.innerText : \"\"')
                    has_recaptcha = 'recaptcha' in body.lower() or '无法连接' in body or 'cannot connect' in body.lower()
                    # 尝试 JS 移除 disabled 并点击 submit 按钮
                    logger.info("Indeed review-module: attempting JS click on submit")
                    # P0修复: 等待按钮动态渲染 (Indeed JS 可能延迟加载)
                    try:
                        page.wait_for_selector(
                            "button[type='submit'], button[aria-label*='Submit'], "
                            "button[aria-label*='Verstuur'], button[aria-label*='Sollicitatie'], "
                            "span[role='button']",
                            timeout=5000
                        )
                        logger.info("Button found after wait")
                    except Exception:
                        logger.info("Button not rendered after 5s wait — trying immediate query")

                    result = page.evaluate('''() => {
                        let btn = document.querySelector('button[type="submit"]');
                        if (!btn) btn = document.querySelector('button[data-test="submit-button"]');
                        if (!btn) btn = document.querySelector('button[aria-label*="Submit"], button[aria-label*="Versturen"], button[aria-label*="Sollicitatie"]');
                        if (!btn) btn = document.querySelector('span[role="button"], button[role="button"]');
                        if (!btn) {
                            const all = document.querySelectorAll('button, span[role="button"], a[role="button"]');
                            for (const b of all) {
                                const t = (b.innerText || b.textContent || '').toLowerCase();
                                if (t.includes('submit') || t.includes('verstuur') || t.includes('versturen')
                                    || t.includes('sollicitatie') || t.includes('apply') || t.includes('send')) {
                                    btn = b; break;
                                }
                            }
                        }
                        if (!btn) return 'no button';
                        btn.removeAttribute('disabled');
                        btn.click();
                        return 'clicked ' + (btn.innerText || btn.textContent || '').slice(0,30);
                    }''')
                    logger.info(f"JS click result: {result}")
                    page.wait_for_timeout(5000)
                    if _check_success_text(page):
                        return True
                    # 点击失败 + 有 reCAPTCHA → 确认被阻断
                    if has_recaptcha:
                        logger.warning("Indeed review-module: reCAPTCHA blocked — skipping job")
                        # P1修复: 关闭 Indeed 页面防止标签页遗留
                        try:
                            logger.info("Closing Indeed page to prevent orphaned tab")
                            page.close()
                        except Exception:
                            pass
                        break

                logger.warning(f"Indeed step {step}: STUCK — triggering AI HELP")
                _request_help(page, profile, resume_path)
                if _wait_for_ai_help(page):
                    logger.info("Indeed: AI help received, retrying...")
                    continue
                else:
                    logger.warning("Indeed: AI help timeout")
                    # 清理残留 flag 防止污染下次请求
                    from pathlib import Path
                    gd_dir = Path(__file__).resolve().parent.parent.parent / "data" / "glassdoor"
                    for f in ["form_help_needed.flag", "form_help_done.flag", "form_help.json", "form_instructions.json"]:
                        try: (gd_dir / f).unlink()
                        except: pass
                    # P1修复: 关闭 Indeed 页面防止标签页遗留
                    try:
                        logger.info("Closing Indeed page after timeout")
                        page.close()
                    except Exception:
                        pass
                    break

        if _check_success_text(page):
            logger.info("Indeed Apply: submitted after step!")
            return True

    return _check_success_text(page)


def _log_page_state(page, label: str):
    """记录页面状态到日志 — 便于 AI 分析。"""
    try:
        url = page.url[:150]
        title = page.title()[:80]
        body = page.evaluate('() => document.body ? document.body.innerText : \"\"')
        # 取前500字符
        body_preview = body[:500].replace('\n', ' | ')
        # 可见表单元素
        els = []
        for el in page.query_selector_all('input:not([type=\"hidden\"]), select, textarea, button'):
            try:
                if el.is_visible():
                    tag = el.evaluate('el => el.tagName')
                    itype = el.get_attribute('type') or ''
                    name = el.get_attribute('name') or ''
                    txt = (el.inner_text() or el.get_attribute('value') or '').strip()[:50]
                    if txt or itype in ('file','select','textarea','radio','checkbox','submit'):
                        els.append(f'{tag}/{itype} name={name[:20]} text="{txt}"')
            except: pass
        logger.info(f"[{label}] Title: {title} | URL: {url}")
        logger.info(f"[{label}] Body: {body_preview[:300]}")
        logger.info(f"[{label}] Elements: {'; '.join(els[:15])}")
    except Exception as e:
        logger.error(f"[{label}] Error: {e}")


# ── 原生 Glassdoor 表单 ──

def _fill_native_glassdoor_form(page: Page, profile: Dict, resume_path: str, max_steps: int) -> bool:
    """填写原生 Glassdoor 多步表单。卡住时触发 AI HELP。"""
    logger.info(f"Native Glassdoor form START: {page.url[:150]}")
    _log_page_state(page, "NATIVE_START")

    for step in range(1, max_steps + 1):
        page.wait_for_timeout(1500)
        if _check_success_text(page):
            logger.info("Glassdoor QA submitted!")
            return True

        logger.info(f"Glassdoor QA step {step}/{max_steps}")
        _log_page_state(page, f"NATIVE_STEP_{step}")
        filled = _fill_form_fields(page, profile, resume_path)
        logger.info(f"Step {step}: filled {filled} fields")

        if not _click_next_or_submit(page):
            if filled == 0:
                logger.warning(f"Step {step}: STUCK — triggering AI HELP")
                _request_help(page, profile, resume_path)
                if _wait_for_ai_help(page):
                    continue
                else:
                    logger.warning("AI help timeout — skipping")
                    from pathlib import Path
                    gd_dir = Path(__file__).resolve().parent.parent.parent / "data" / "glassdoor"
                    for f in ["form_help_needed.flag", "form_help_done.flag", "form_help.json", "form_instructions.json"]:
                        try: (gd_dir / f).unlink()
                        except: pass
                    break

        if _check_success_text(page):
            return True

    return _check_success_text(page)


# ── 通用表单填写 ──

def _fill_form_fields(obj, profile: Dict, resume_path: str) -> int:
    """在 page 或 frame 中填写表单。返回填写的字段数。"""
    filled = 0
    page = obj if isinstance(obj, Page) else obj.page

    # SELECT
    for el in obj.query_selector_all("select"):
        try:
            if not el.is_visible(): continue
            label = _get_label(el, page)
            label_lower = label.lower()
            if any(kw in label_lower for kw in ["country", "land", "phone code", "landcode"]):
                opts = el.evaluate("el => Array.from(el.options).map(o => ({t: o.text, v: o.value}))")
                for o in opts:
                    ot = o["t"].lower()
                    if "carib" in ot: continue
                    desired_country = str(profile.get("country", "") or "").casefold()
                    if desired_country and desired_country in ot:
                        el.select_option(label=o["t"])
                        logger.info(f"Selected: {o['t'][:40]}")
                        filled += 1; break
        except Exception: continue

    # 文本输入
    for el in obj.query_selector_all("input:not([type='hidden']):not([type='submit']):not([type='file']):not([type='radio']):not([type='checkbox'])"):
        try:
            if not el.is_visible(): continue
            label = _get_label(el, page)
            l = label.lower()
            if any(kw in l for kw in ["first", "voornaam"]):
                value = str(profile.get("first_name", "") or "")
                if value: el.fill(value); filled += 1
            elif any(kw in l for kw in ["last", "achternaam"]):
                value = str(profile.get("last_name", "") or "")
                if value: el.fill(value); filled += 1
            elif any(kw in l for kw in ["email", "e-mail"]):
                value = str(profile.get("email", "") or "")
                if value: el.fill(value); filled += 1
            elif any(kw in l for kw in ["phone", "telefoon", "mobiel"]):
                value = str(profile.get("phone", "") or "")
                if value: el.fill(value); filled += 1
            elif any(kw in l for kw in ["city", "stad", "woonplaats", "location", "plaats", "locatie"]):
                el.click(); el.fill("")
                value = str(profile.get("city", "") or "")
                if not value: continue
                el.type(value, delay=100)
                page.wait_for_timeout(1000)
                try: page.keyboard.press("ArrowDown"); page.wait_for_timeout(300); page.keyboard.press("Enter")
                except: pass
                filled += 1
            elif any(kw in l for kw in ["linkedin"]):
                value = str(profile.get("linkedin_url", "") or "")
                if value: el.fill(value); filled += 1
            elif any(kw in l for kw in ["country", "land"]):
                value = str(profile.get("country", "") or "")
                if value: el.fill(value); filled += 1
            elif any(kw in l for kw in ["address", "street", "straat", "adres"]):
                value = str(profile.get("address", "") or "")
                if value: el.fill(value); filled += 1
            elif any(kw in l for kw in ["postal", "zip", "postcode"]):
                value = str(profile.get("postal_code", "") or "")
                if value: el.fill(value); filled += 1
        except Exception: continue

    # ── name 属性匹配 (P2: Indeed jobTitle/companyName 无 label) ──
    for el in obj.query_selector_all("input:not([type='hidden']):not([type='submit']):not([type='file']):not([type='radio']):not([type='checkbox'])"):
        try:
            if not el.is_visible(): continue
            val = el.get_attribute('value') or el.evaluate('el => el.value')
            if val and val.strip(): continue
            el_name = (el.get_attribute("name") or "").lower()
            if not el_name: continue
            if "jobtitle" in el_name or "job_title" in el_name or "functie" in el_name:
                value = str(profile.get("current_job_title", "") or "")
                if value:
                    el.fill(value)
                    logger.info("Filled configured current job title")
                    filled += 1
            elif "companyname" in el_name or "company_name" in el_name or "bedrijf" in el_name or "employer" in el_name:
                value = str(profile.get("current_company", "") or "")
                if value:
                    el.fill(value)
                    logger.info("Filled configured current company")
                    filled += 1
        except Exception: continue

    # 兜底：填所有仍为空的可见输入框
    for el in obj.query_selector_all("input:not([type='hidden']):not([type='submit']):not([type='file']):not([type='radio']):not([type='checkbox'])"):
        try:
            if not el.is_visible(): continue
            val = el.get_attribute('value') or el.evaluate('el => el.value')
            if val and val.strip(): continue  # 已有值
            label = _get_label(el, page)
            # 空字段填合理默认值
            el.fill("N/A")
            logger.info(f"Default fill: label='{label[:40]}'")
            filled += 1
        except Exception: continue

    # Radio (Yes/Ja)
    for el in obj.query_selector_all("input[type='radio']"):
        try:
            if not el.is_visible() or el.is_checked(): continue
            val = (el.get_attribute("value") or "").lower()
            if val in ("yes", "ja", "1", "true"):
                el.click(force=True); page.wait_for_timeout(300); filled += 1
        except Exception: continue

    # Checkbox
    for el in obj.query_selector_all("input[type='checkbox']"):
        try:
            if not el.is_visible() or el.is_checked(): continue
            el.check(force=True); page.wait_for_timeout(300); filled += 1
        except Exception: continue

    # TEXTAREA — 复杂问题交给 AI 处理 (cover letter → 专用 flag)
    textarea_count = 0
    for el in obj.query_selector_all("textarea"):
        try:
            if not el.is_visible(): continue
            label = _get_label(el, page) if page else ""
            if _is_cover_letter_textarea(el, label):
                logger.info(f"📝 Cover Letter textarea detected: '{label[:60]}'")
                cl_result = _trigger_glassdoor_cl_request(page, profile, label, field_type="textarea")
                cl_text = str((cl_result or {}).get("cover_letter_text", "")).strip()
                if cl_text:
                    el.fill(cl_text)
                    logger.info("Cover letter text filled")
                    filled += 1
                continue
            textarea_count += 1
        except Exception: continue
    if textarea_count > 0:
        logger.info(f"Found {textarea_count} textarea(s) — will need AI help if stuck")

    # SELECT (通用下拉 — 非国家代码)
    for el in obj.query_selector_all("select"):
        try:
            if not el.is_visible(): continue
            label = _get_label(el, page).lower()
            if any(kw in label for kw in ["country", "land", "phone"]):
                continue  # 已处理
            # 学历选择
            if any(kw in label for kw in ["education", "opleiding", "degree", "niveau", "diploma"]):
                opts = el.evaluate("el => Array.from(el.options).map(o => o.text)")
                # 找最高学历
                for target in ["WO", "Master", "University", "HBO", "Bachelor"]:
                    for o in opts:
                        if target.lower() in o.lower():
                            el.select_option(label=o)
                            logger.info(f"Selected education: {o[:40]}")
                            filled += 1; break
                    if filled > len([x for x in obj.query_selector_all("select") if x.is_visible()]) - 1:
                        break
                else:
                    # 选最后一个（通常最高学历）
                    if len(opts) > 1:
                        el.select_option(index=len(opts)-1)
                        logger.info(f"Selected last education option: {opts[-1][:40]}")
                        filled += 1
            else:
                # 其他 SELECT：选第一个非空选项
                opts = el.evaluate("el => Array.from(el.options).map(o => o.text)")
                for o in opts[1:]:  # 跳过 placeholder
                    if o.strip() and "select" not in o.lower():
                        el.select_option(label=o)
                        logger.info(f"Selected: {o[:40]}")
                        filled += 1; break
        except Exception: continue

    # CV / Cover Letter upload
    for el in obj.query_selector_all("input[type='file']"):
        try:
            label = _get_label(el, page) if page else ""
            el_id = (el.get_attribute("id") or "").lower()
            el_name = (el.get_attribute("name") or "").lower()
            combined = f"{label} {el_id} {el_name}".lower()
            if any(kw in combined for kw in CL_TEXTAREA_KW + ["sollicitatiebrief"]):
                logger.info(f"📝 Cover Letter file upload detected: '{label[:60]}'")
                cl_result = _trigger_glassdoor_cl_request(page, profile, label or el_name or el_id, field_type="file")
                cl_path = str((cl_result or {}).get("cover_letter_docx_path") or (cl_result or {}).get("cover_letter_path") or "").strip()
                if cl_path:
                    el.set_input_files(cl_path)
                    page.wait_for_timeout(1000)
                    logger.info(f"Cover letter DOCX uploaded: {cl_path}")
                    filled += 1
                continue

            el.set_input_files(resume_path); page.wait_for_timeout(1000)
            logger.info("CV uploaded"); filled += 1
        except Exception: continue

    return filled


def _click_next_or_submit(obj) -> bool:
    """点击下一步/提交按钮 — 只点推进表单的按钮，不点 Sluiten/Probleem melden 等。"""
    # 排除的关键词（关闭、报告问题等）
    skip_texts = ["sluiten", "probleem melden", "close", "cancel", "annuleren", "terug", "back"]
    for text in ["Doorgaan", "Sollicitatie versturen", "Versturen", "Submit", "Verzenden",
                 "Submit your application", "Submit application", "Apply now",
                 "Next", "Volgende", "Continue", "Send", "Review", "Done", "Klaar",
                 "Solliciteer", "Apply", "Opslaan"]:
        for btn in obj.query_selector_all("button, input[type='submit']"):
            try:
                if hasattr(btn, 'is_disabled') and btn.is_disabled(): continue
                t = (btn.inner_text() or btn.get_attribute("value") or "").strip().lower()
                if any(s in t for s in skip_texts): continue
                if text.lower() in t:
                    if btn.is_visible():
                        btn.click(force=True); obj.wait_for_timeout(2000); return True
                    else:
                        # 回退: 按钮可能存在但被隐藏（如 resume 页 Doorgaan visible=false）
                        try:
                            btn.click(force=True)
                            obj.wait_for_timeout(2000)
                            return True
                        except Exception: continue
            except Exception: continue
    return False


def _check_success_text(obj) -> bool:
    """检查是否提交成功。"""
    try:
        # URL 检测 — post-apply 或 smart-apply-action=POST_AP
        url = (obj.url if hasattr(obj, 'url') else '').lower()
        if "post-apply" in url or "smart-apply-action=post_ap" in url:
            logger.info("SUCCESS: post-apply page reached!")
            return True
        body = obj.inner_text("body")[:3000].lower()
        # 排除 "Nog één stap" — 这不是真正的完成页
        if "nog één stap" in body or "nog een stap" in body or "one more step" in body:
            logger.info("Detected 'one more step' page — NOT success, needs further action")
            return False
        for kw in ["thank you", "bedankt", "application submitted", "successfully",
                     "sollicitatie ontvangen", "post-apply", "verzonden"]:
            if kw in body: return True
    except Exception: pass
    return False


def _get_label(el, page=None) -> str:
    """获取表单元素的 label。"""
    try:
        el_id = el.get_attribute("id") or ""
        if el_id and page:
            try:
                le = page.evaluate_handle(f"() => document.querySelector(\"label[for='{el_id}']\")")
                try: return le.inner_text().strip()[:60]
                except: pass
            except: pass
        for attr in ["aria-label", "name", "placeholder"]:
            v = el.get_attribute(attr) or ""
            if v: return v[:60]
    except Exception: pass
    return ""


def _wait_for_ai_help(page: Page) -> bool:
    """等待 AI 响应 HELP FLAG，读取并执行 AI 指令。"""
    from pathlib import Path
    data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "glassdoor"
    data_dir.mkdir(parents=True, exist_ok=True)
    done_flag = data_dir / "form_help_done.flag"
    instructions_file = data_dir / "form_instructions.json"

    waited = 0
    while not done_flag.exists():
        time.sleep(2)
        waited += 2
        if waited % 30 == 0:
            logger.info(f"   ...waiting for AI help ({waited}s)")
        if waited > 120:
            logger.warning("AI help timeout (2min)")
            return False

    # 读取并执行 AI 指令
    if instructions_file.exists():
        try:
            import json
            with open(instructions_file, "r", encoding="utf-8") as f:
                instructions = json.load(f)
            if instructions.get("needs_user"):
                logger.warning(f"AI needs user input: {instructions.get('questions', [])}")
                try: done_flag.unlink()
                except: pass
                try: instructions_file.unlink()
                except: pass
                return False
            actions = instructions.get("actions", [])
            logger.info(f"Executing {len(actions)} AI instruction(s)")
            for action in actions:
                try:
                    _execute_ai_action(page, action)
                except Exception as e:
                    logger.warning(f"AI action failed: {action.get('type')} — {e}")
        except Exception as e:
            logger.error(f"Failed to read AI instructions: {e}")
        # 清理
        try: done_flag.unlink()
        except: pass
        try: instructions_file.unlink()
        except: pass
    return True


def _execute_ai_action(page: Page, action: dict):
    """执行单条 AI 指令（简化版，不依赖 easy_apply）。"""
    atype = action.get("type", "")
    target = action.get("target", "")
    value = action.get("value", "")
    logger.info(f"  AI: {atype} | {target[:50]} = {value[:50]}")

    if atype == "fill_input":
        for attr in ["name", "aria-label", "placeholder"]:
            el = page.query_selector(f'input[{attr}*="{target}"]') if target else None
            if el and el.is_visible():
                el.click(); el.fill(""); el.type(str(value), delay=30)
                page.wait_for_timeout(300); return
        # 按 label 查找
        for inp in page.query_selector_all("input:not([type='hidden'])"):
            if inp.is_visible():
                label = _get_label(inp, page)
                if target.lower() in label.lower():
                    inp.click(); inp.fill(""); inp.type(str(value), delay=30)
                    page.wait_for_timeout(300); return

    elif atype == "click_button":
        # 支持 <button>, <a>, [role="button"], input[type='submit']
        for selector in ["button", "a", "[role='button']", "input[type='submit']"]:
            for el in page.query_selector_all(selector):
                if not el.is_visible():
                    continue
                el_text = (el.inner_text() or "").strip().lower()
                el_aria = (el.get_attribute("aria-label") or "").lower()
                if value.lower() in el_text or value.lower() in el_aria:
                    try:
                        if not el.is_disabled():
                            el.click(force=True)
                            page.wait_for_timeout(2000)
                            return
                    except Exception:
                        el.click(force=True)
                        page.wait_for_timeout(2000)
                        return

    elif atype == "select_option":
        for sel in page.query_selector_all("select"):
            if sel.is_visible():
                label = _get_label(sel, page)
                if target.lower() in label.lower():
                    sel.select_option(label=value)
                    page.wait_for_timeout(300); return

    elif atype == "click_radio":
        for r in page.query_selector_all("input[type='radio']"):
            if r.is_visible() and value.lower() in (r.get_attribute('value') or '').lower():
                r.click(force=True)
                page.wait_for_timeout(300); return

    elif atype == "click_checkbox":
        for cb in page.query_selector_all("input[type='checkbox']"):
            cb_name = (cb.get_attribute('name') or '').lower()
            if value.lower() in cb_name:
                if not cb.is_checked():
                    cb.click(force=True)
                    page.wait_for_timeout(300)
                return

    elif atype == "upload_file":
        from pathlib import Path
        fp = str(Path(value).resolve())
        target_lower = target.lower()
        chosen = None
        for inp in page.query_selector_all("input[type='file']"):
            label = _get_label(inp, page).lower()
            attrs = " ".join([
                inp.get_attribute("id") or "",
                inp.get_attribute("name") or "",
                inp.get_attribute("aria-label") or "",
                label,
            ]).lower()
            if target_lower and target_lower in attrs:
                chosen = inp
                break
            if chosen is None:
                chosen = inp
        if chosen:
            chosen.set_input_files(fp)
            page.wait_for_timeout(1000)
            return

    elif atype == "submit_form":
        # JS 提交表单 — 绕过 reCAPTCHA disabled 按钮
        logger.info(f"  AI: submit_form — executing JS form.submit()")
        result = page.evaluate("() => { const f = document.querySelector('form'); if (f) { f.submit(); return 'submitted'; } return 'no form'; }")
        logger.info(f"  AI: submit_form result: {result}")
        page.wait_for_timeout(3000)

    elif atype == "press_key":
        page.keyboard.press(value)
        page.wait_for_timeout(300)

    elif atype == "wait":
        page.wait_for_timeout(int(value) * 1000 if value else 1000)


def _request_help(page: Page, profile: Dict, resume_path: str):
    """AI 帮助请求 — 提取完整页面状态供 Agent 分析。"""
    import json
    from datetime import datetime
    from pathlib import Path

    data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "glassdoor"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 提取页面标题作为上下文 (P1-3: Agent 需要知道在投什么岗位)
    try:
        page_title = page.title()[:200]
    except Exception:
        page_title = ""

    state = _extract_form_state(page)
    help_data = {
        "form_state": (
            f"=== GLASSDOOR FORM ===\n"
            f"Page Title: {page_title}\n"
            f"URL: {page.url[:150]}\n"
            f"{state}"
        ),
        "profile": profile, "url": page.url, "source": "glassdoor",
        "timestamp": datetime.now().isoformat(),
    }
    # 清理旧的 done flag 和指令文件, 防止污染本次请求 (P0-1)
    for old_f in ["form_help_done.flag", "form_instructions.json"]:
        try:
            fp = data_dir / old_f
            if fp.exists(): fp.unlink()
        except FileNotFoundError: pass

    with open(data_dir / "form_help.json", "w", encoding="utf-8") as f:
        json.dump(help_data, f, ensure_ascii=False, indent=2)
    (data_dir / "form_help_needed.flag").write_text(datetime.now().isoformat(), encoding="utf-8")
    logger.info("Glassdoor AI help requested")


def _extract_form_state(obj) -> str:
    lines = []
    # 页面可见文本和 HTML 摘要 (帮助 Agent 理解岗位上下文, P1-3)
    try:
        body_text = obj.evaluate('() => document.body ? document.body.innerText : ""')
        if body_text:
            lines.append("--- PAGE CONTEXT (first 3000 chars) ---")
            lines.append(body_text[:3000])
            lines.append("--- FORM ELEMENTS ---")
    except Exception:
        pass

    try:
        html = obj.evaluate('() => document.body ? document.body.outerHTML : ""')
        if html:
            lines.append("--- PAGE HTML EXCERPT ---")
            lines.append(" ".join(html.split())[:5000])
    except Exception:
        pass

    for el in obj.query_selector_all("input:not([type='hidden']), select, textarea, button"):
        try:
            if not el.is_visible(): continue
            tag = el.evaluate("el => el.tagName")
            itype = el.get_attribute("type") or ""
            name = el.get_attribute("name") or ""
            value = el.get_attribute("value") or ""
            label = _get_label(el)
            txt = (el.inner_text() or "").strip()[:50]
            lines.append(f"[{tag}/{itype}] name='{name[:30]}' value='{value[:30]}' label='{label[:60]}' text='{txt}'")
        except Exception: pass
    return "\n".join(lines)


def is_quick_apply_available(page: Page) -> bool:
    for btn in page.query_selector_all("button"):
        try:
            if btn.is_visible() and "snelsollicitatie" in (btn.inner_text() or "").lower(): return True
        except: continue
    return False


def is_external_apply(page: Page) -> bool:
    for el in page.query_selector_all("a, button"):
        try:
            if not el.is_visible(): continue
            txt = (el.inner_text() or "").strip().lower()
            # 检查按钮文本
            if any(kw in txt for kw in ["solliciteren op site", "apply on company", "apply externally",
                                         "solliciteer op de website", "op de website van"]):
                return True
            href = (el.get_attribute("href") or "").lower()
            if ("apply" in txt or "solliciteer" in txt) and href.startswith("http") and "glassdoor" not in href:
                return True
        except: continue
    return False


# ═══════════════════════════════════════════════════════════════
# Cover Letter 检测 (Glassdoor — 非阻塞, Agent 异步生成)
# ═══════════════════════════════════════════════════════════════

CL_TEXTAREA_KW = [
    "cover letter", "cover_letter", "coverletter",
    "motivation", "motivatie", "motivatiebrief",
    "why do you want", "tell us about yourself",
    "additional information", "personal statement",
]


def _is_cover_letter_textarea(el, label: str = "") -> bool:
    label_lower = (label or "").lower()
    if any(kw in label_lower for kw in CL_TEXTAREA_KW):
        return True
    try:
        el_id = (el.get_attribute("id") or "").lower()
        el_name = (el.get_attribute("name") or "").lower()
        placeholder = (el.get_attribute("placeholder") or "").lower()
        combined = f"{el_id} {el_name} {placeholder}"
        if any(kw in combined for kw in CL_TEXTAREA_KW):
            return True
    except Exception:
        pass
    return False


def _trigger_glassdoor_cl_request(page, profile: dict, field_label: str = "", field_type: str = "textarea_or_file"):
    """创建 Glassdoor cover_letter_needed.flag；Codex 模式下等待并返回结果。"""
    from src.cover_letter_agent import request_and_wait_cover_letter
    try:
        jd_text = ""
        job_title = ""
        company = ""
        try:
            jd_text = page.evaluate("() => document.body ? document.body.innerText : ''")[:5000]
        except Exception:
            pass
        try:
            job_title = page.evaluate(
                """() => {
                    const el = document.querySelector('[class*="job-title"], [class*="JobTitle"], h1');
                    return el ? el.innerText : '';
                }"""
            ).strip()
        except Exception:
            pass
        try:
            company = page.evaluate(
                """() => {
                    const el = document.querySelector('[class*="company"], [class*="employer"]');
                    return el ? el.innerText : '';
                }"""
            ).strip()
        except Exception:
            pass

        return request_and_wait_cover_letter(
            platform="glassdoor",
            profile=profile,
            jd_text=jd_text,
            job_title=job_title,
            company=company,
            cl_field_info={"type": field_type, "label": field_label},
        )
    except Exception as e:
        logger.warning(f"Cover letter flag creation error: {e}")
        return None
