import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / 'external_browser.json'
logger = logging.getLogger('linkedin_autoapply.external_browser')


class ExternalApplyHandoff(RuntimeError):
    pass


class ExternalApplyUnconfirmed(RuntimeError):
    pass


def focus_external_browser(page=None, process_id=None):
    """Restore and foreground Edge for a human handoff.

    This only changes window focus. It never clicks a challenge, changes the
    page, or attempts to obtain a CAPTCHA token.
    """
    if os.name != 'nt':
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        host = ''
        try:
            host = (urlsplit(page.url).hostname or '').casefold() if page is not None else ''
        except Exception:
            pass
        host_tokens = [x for x in re.split(r'[.\\-]', host) if len(x) >= 3]
        candidates = []

        def image_name(pid):
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ''
            try:
                length = wintypes.DWORD(1024)
                buffer = ctypes.create_unicode_buffer(length.value)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                    return buffer.value.casefold()
            finally:
                kernel32.CloseHandle(handle)
            return ''

        def callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, len(class_name))
            if class_name.value != 'Chrome_WidgetWin_1':
                return True
            pid_value = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
            pid = int(pid_value.value)
            if process_id and pid != int(process_id):
                return True
            executable = image_name(pid)
            if executable and not executable.endswith('msedge.exe'):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(max(1, length + 1))
            user32.GetWindowTextW(hwnd, title, len(title))
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            title_text = title.value.casefold()
            score = (100 if process_id else 0) + sum(10 for token in host_tokens if token in title_text)
            candidates.append((score, area, hwnd, pid, title.value))
            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        if not candidates:
            logger.warning('External browser handoff: no visible Edge window found')
            return False
        _, _, hwnd, pid, title = max(candidates, key=lambda item: (item[0], item[1]))
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        # Windows can reject SetForegroundWindow when the caller is not the
        # current foreground process. Temporarily attach the caller's input
        # queue to the foreground queue, then verify the actual foreground
        # HWND. This only changes window focus; it never interacts with the
        # page or the challenge widget.
        focused = bool(user32.SetForegroundWindow(hwnd))
        if not focused:
            foreground = user32.GetForegroundWindow()
            foreground_pid = wintypes.DWORD()
            foreground_thread = user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid)) if foreground else 0
            current_thread = kernel32.GetCurrentThreadId()
            attached = False
            try:
                if foreground_thread and foreground_thread != current_thread:
                    attached = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
                user32.BringWindowToTop(hwnd)
                user32.SetActiveWindow(hwnd)
                focused = bool(user32.SetForegroundWindow(hwnd))
                if not focused:
                    switch = getattr(user32, 'SwitchToThisWindow', None)
                    if switch is not None:
                        switch(hwnd, True)
                    focused = user32.GetForegroundWindow() == hwnd
            finally:
                if attached:
                    user32.AttachThreadInput(current_thread, foreground_thread, False)
        logger.info('External browser handoff: Edge window restored and foregrounded (pid=%s, title=%s, focused=%s)', pid, title[:120], focused)
        return focused
    except Exception as error:
        logger.warning('External browser handoff: unable to foreground Edge: %s', type(error).__name__)
        return False


RECEIPT_PHRASES = (
    'thank you for applying', 'we have received your application', 'thanks for applying',
    'application submitted', 'application received', 'your application has been submitted',
    'application has been successfully submitted', 'your application has been successfully submitted',
    'your application has been received', 'application successfully submitted',
    'sollicitatie ontvangen', 'bedankt voor je sollicitatie', 'bedankt voor uw sollicitatie',
    'bewerbung eingegangen',
)


def visible_receipts(page):
    found = set()
    for frame in page.frames:
        try:
            body = frame.locator('body').inner_text(timeout=3000).lower()
            found.update(phrase for phrase in RECEIPT_PHRASES if phrase in body)
        except Exception:
            continue
    return found


@dataclass
class ExternalSession:
    page: object
    backend: str
    fallback_reason: str = ''


def load_settings():
    settings = json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))
    settings['backend'] = os.environ.get('LINKEDIN_EXTERNAL_BROWSER', settings['backend']).lower()
    if settings['backend'] not in ('edge', 'lightpanda'):
        raise ValueError('External browser must be edge or lightpanda')
    return settings


def record_outcome(session, status, platform, reason=''):
    url = urlsplit(session.page.url)
    entry = {'time': datetime.now(timezone.utc).isoformat(), 'platform': platform,
             'backend': session.backend, 'status': status, 'reason': reason,
             'host': url.hostname, 'path': url.path, 'fallback_reason': session.fallback_reason}
    log_path = ROOT / 'logs/external_browser.jsonl'
    try:
        with log_path.open('a', encoding='utf-8') as output:
            output.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        logger.exception('Could not record external browser outcome')
    logger.info('External browser=%s status=%s reason=%s', session.backend, status, reason)


def blocking_reason(page):
    def actionable_challenge(node):
        """Require a genuinely visible challenge in the current viewport.

        CAPTCHA libraries often keep hidden/placeholder iframes in the DOM.
        ``Locator.is_visible()`` alone treats some of those frames as visible
        even though a person cannot interact with them.
        """
        try:
            return bool(node.evaluate("""e => {
              const r=e.getBoundingClientRect(), s=getComputedStyle(e);
              const viewport=r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth;
              return !!(r.width>=30 && r.height>=30 && viewport &&
                s.display!=='none' && s.visibility!=='hidden' && s.opacity!=='0');
            }"""))
        except Exception:
            return False

    for index, frame in enumerate(page.frames):
        logger.info("External blocking check: inspecting frame %d", index)
        if frame != page.main_frame and frame.url in ('', 'about:blank'):
            continue
        logger.info('External blocking check: inspecting password fields in frame %d', index)
        if any(field.is_visible() for field in frame.locator('input[type=password]').all()):
            return 'authentication_required'
        for selector in ('iframe[src*="recaptcha"][title*="challenge" i]',
                         'iframe[src*="recaptcha"][src*="/bframe"]',
                         'iframe[src*="hcaptcha"][title*="challenge" i]',
                         '[id*="challenge-stage"]'):
            logger.info('External blocking check: inspecting challenge selector in frame %d: %s', index, selector)
            if any(actionable_challenge(field) for field in frame.locator(selector).all()):
                return 'captcha_required'
        try:
            logger.info('External blocking check: reading body in frame %d', index)
            text = frame.locator('body').inner_text(timeout=3000).lower()
        except Exception:
            if frame == page.main_frame:
                raise
            continue
        if any(message in text for message in ('verify you are human', 'verify that you are human',
                                               'complete the captcha', 'checking your browser')):
            return 'captcha_required'
    return ''


def _new_lightpanda_context(browser, settings):
    context = browser.new_context()
    context.set_default_timeout(settings['action_timeout_ms'])
    context.set_default_navigation_timeout(settings['navigation_timeout_ms'])
    context._external_backend = 'lightpanda'
    return context


def _install_external_diagnostics(page):
    """Collect non-sensitive browser/runtime evidence for backend fallback."""
    context = page.context
    if getattr(context, '_external_diagnostics_installed', False):
        return
    context._external_diagnostics_installed = True
    context._external_page_errors = []
    context._external_network_events = []

    def page_error(error):
        stack = getattr(error, 'stack', '') or ''
        context._external_page_errors.append((str(error) + '\n' + str(stack))[:4000])
        context._external_page_errors = context._external_page_errors[-50:]

    def request(request):
        if request.method.upper() not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            return
        entry = {'kind': 'request', 'method': request.method.upper(),
                 'url': urlsplit(request.url)._replace(query='', fragment='').geturl()}
        context._external_network_events.append(entry)
        context._external_network_events = context._external_network_events[-100:]

    def response(response):
        method = response.request.method.upper()
        if method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            return
        entry = {'kind': 'response', 'method': method, 'status': response.status,
                 'url': urlsplit(response.url)._replace(query='', fragment='').geturl()}
        context._external_network_events.append(entry)
        context._external_network_events = context._external_network_events[-100:]

    page.on('pageerror', page_error)
    page.on('request', request)
    page.on('response', response)


@contextmanager
def application_page(source_page):
    settings = load_settings()
    if settings['backend'] == 'edge':
        _install_external_diagnostics(source_page)
        yield ExternalSession(source_page, 'edge')
        return
    target = urlsplit(source_page.url)
    excluded_hosts = ('linkedin.com', 'glassdoor.com', 'glassdoor.nl', 'github.com', 'google.com', 'bing.com')
    if target.scheme not in ('http', 'https') or any(
            target.hostname == host or (target.hostname or '').endswith('.' + host) for host in excluded_hosts):
        raise ExternalApplyHandoff('invalid_external_destination')
    browser = context = None
    try:
        browser_type = source_page.context.browser.browser_type
        try:
            browser = browser_type.connect_over_cdp(settings['endpoint'], timeout=5000)
        except Exception:
            if not settings['autostart']:
                raise
            subprocess.run([sys.executable, settings['manager_path'], 'start'],
                           check=True, timeout=45, capture_output=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            browser = browser_type.connect_over_cdp(settings['endpoint'], timeout=10000)
        context = _new_lightpanda_context(browser, settings)
        page = context.new_page()
        _install_external_diagnostics(page)
        page.goto(source_page.url, wait_until='domcontentloaded')
    except Exception as error:
        if context:
            context.close()
        if browser:
            browser.close()
        if not settings['fallback_before_actions']:
            raise
        logger.warning('Lightpanda setup failed; retaining original browser: %s', error)
        yield ExternalSession(source_page, 'edge', type(error).__name__)
        return
    try:
        logger.info('External application routed to Lightpanda: %s', target.hostname)
        yield ExternalSession(page, 'lightpanda')
    finally:
        try:
            context.close()
        finally:
            browser.close()


def set_file_input(element, file_path):
    path = Path(file_path)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve(strict=True)
    element.set_input_files({'name': path.name,
                            'mimeType': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                            'buffer': path.read_bytes()})


def _action_scope(page, target):
    match = re.match(r'^frame\[(\d+)\]::(.*)$', target, re.S)
    if not match:
        return page.main_frame, target
    index = int(match.group(1))
    if index >= len(page.frames):
        raise ValueError(f'Frame index no longer exists: {index}')
    return page.frames[index], match.group(2)


def _find_field(scope, target, selector):
    fields = scope.locator(selector)
    if target.startswith('css='):
        css = target[4:]
        # IDs generated by ATSs frequently contain dots, which are CSS class
        # separators unless escaped. Prefer an exact literal ID when present.
        if css.startswith('#') and re.fullmatch(r'#[\w.:-]+', css):
            exact_id = scope.locator('[id=' + json.dumps(css[1:]) + ']')
            if exact_id.count() == 1:
                return exact_id
        return scope.locator(css)
    if target.startswith(('#', '.', '[')) or re.match(r'^(input|textarea|select|button|div|form)[.#\[:]', target):
        return scope.locator(target)
    if target:
        matches = []
        for field in fields.all():
            attrs = [field.get_attribute(name) or '' for name in ('name', 'id', 'aria-label', 'placeholder')]
            if target in attrs:
                matches.append(field)
        if len(matches) == 1:
            return matches[0]
        # Some ATSs (including Ashby) render a single unlabeled autocomplete
        # after a visible text heading. The AI can name that heading, but it is
        # not an HTML label. A lone control is still safe to use.
        if selector == 'input[role=combobox]' and fields.count() == 1:
            return fields
        by_label = scope.get_by_label(target, exact=True)
        if by_label.count() == 1:
            return by_label
        by_label = scope.get_by_label(target, exact=False)
        if by_label.count() == 1 and by_label.evaluate('(el, selector) => el.matches(selector)', selector):
            return by_label
        for field in fields.all():
            attrs = ' '.join(field.get_attribute(name) or '' for name in ('name', 'id', 'aria-label', 'placeholder'))
            if target.lower() in attrs.lower():
                matches.append(field)
        if len(matches) == 1:
            return matches[0]
    elif fields.count() == 1:
        return fields
    raise ValueError(f'Field missing or ambiguous: {target}')


def _check_control(page, field):
    if getattr(page.context, '_external_backend', 'edge') != 'lightpanda':
        field.check()
        return
    if field.count() != 1 or not field.is_enabled():
        raise ValueError('Choice is missing, ambiguous, hidden or disabled')
    if not field.is_visible():
        clicked = field.evaluate("""el => {
            if (el.checked) return true;
            const labels = Array.from(el.labels || []).filter(label => {
                const style = getComputedStyle(label), rect = label.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            });
            if (labels.length !== 1) return false;
            labels[0].click();
            return !!el.checked;
        }""")
        if not clicked:
            raise ValueError('Hidden choice has no unique visible associated label or selection failed')
        return
    checked = field.evaluate("""el => {
        if (!(el.checked || el.getAttribute('aria-checked') === 'true')) el.click();
        return !!el.checked || el.getAttribute('aria-checked') === 'true';
    }""")
    if not checked:
        raise RuntimeError('Choice selection was not confirmed')


def _fill_control(page, field, value):
    """Fill a normal control, keeping React state in sync in Lightpanda."""
    if field.count() != 1 or not field.is_visible():
        raise ValueError('Field is hidden or unavailable; open the application form before filling')
    if getattr(page.context, '_external_backend', 'edge') != 'lightpanda':
        field.fill(value)
        return
    applied = field.evaluate(r'''(el, value) => {
        const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        el.focus();
        setter.call(el, value);
        el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        el.blur();
        return el.value === value;
    }''', value)
    if not applied:
        raise RuntimeError('React field did not retain its value')


def _question_radios(scope, question):
    """Return the unique radio group whose enclosing question contains the text."""
    if not question:
        raise ValueError('Radio question is required')
    radios = scope.locator('input[type=radio], [role=radio]').all()
    matches = []
    for radio in radios:
        matched = radio.evaluate(r'''(el, question) => {
            const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const name = el.getAttribute('name');
            for (let p = el.parentElement; p; p = p.parentElement) {
                const choices = Array.from(p.querySelectorAll('input[type=radio], [role=radio]'));
                const groups = new Set(choices.map(x => x.getAttribute('name') || ''));
                if (groups.size > 1) return false;
                if (choices.length > 1 && groups.size === 1 && norm(p.innerText).includes(norm(question))) return true;
            }
            return false;
        }''', question)
        if matched:
            matches.append(radio)
    names = {r.get_attribute('name') for r in matches}
    if not matches or len(names) != 1 or not next(iter(names)):
        raise ValueError(f'Radio question missing or ambiguous: {question}')
    return matches


def execute_action(page, action):
    kind, target, value = action.get('type', ''), action.get('target', ''), str(action.get('value', ''))
    scope, target = _action_scope(page, target)
    if kind == 'wait':
        page.wait_for_timeout(min(30000, max(0, int(float(value or '1') * 1000))))
        return
    reason = blocking_reason(page)
    if reason:
        raise ExternalApplyHandoff(reason)
    if getattr(page.context, '_external_submit_attempted', False):
        raise ExternalApplyUnconfirmed('submission_already_attempted')
    page.context._external_actions_started = True
    if kind == 'fill_input':
        field = _find_field(scope, target, 'input:not([type=hidden]):not([type=file]), textarea')
        _fill_control(page, field, value)
    elif kind == 'select_option':
        field = _find_field(scope, target, 'select')
        options = field.locator('option')
        values = options.evaluate_all('(items) => items.map(x => x.value)')
        field.select_option(value=value) if value in values else field.select_option(label=value)
    elif kind == 'upload_file':
        field = _find_field(scope, target, 'input[type=file]')
        set_file_input(field, value)
        filename = Path(value).name
        selected = field.count() > 0 and field.evaluate('(el, name) => el.files.length > 0 && el.files[0].name === name', filename)
        displayed = any(element.is_visible() for element in scope.get_by_text(filename, exact=True).all())
        if not selected and not displayed:
            raise RuntimeError('File selection was not confirmed')
    elif kind == 'click_checkbox':
        choice = value if value and value.lower() not in ('true', 'yes', '1', 'on') else target
        _check_control(page, _find_field(scope, choice or target, 'input[type=checkbox]'))
    elif kind in ('click_radio', 'click_radio_by_index'):
        if kind == 'click_radio_by_index':
            group = _question_radios(scope, target)
            index = int(value)
            if index < 0 or index >= len(group):
                raise ValueError('Radio index outside question group')
            _check_control(page, group[index])
        else:
            candidates = scope.locator('input[type=radio], [role=radio]').all()
            matches = [field for field in candidates
                       if (not target or target in (field.get_attribute('name'), field.get_attribute('id')))
                       and (field.get_attribute('value') or '').lower() == value.lower()]
            if len(matches) != 1 and target:
                group = _question_radios(scope, target)
                normalized = value.lower().strip()
                matches = [field for field in group if field.evaluate('''(el, answer) => {
                    const choices = [el.value, ...Array.from(el.labels || []).map(x => x.innerText.trim())];
                    return choices.some(x => (x || '').toLowerCase() === answer) ||
                        (answer === 'yes' && el.value === 'true') || (answer === 'no' && el.value === 'false');
                }''', normalized)]
            if len(matches) != 1:
                raise ValueError(f'Radio choice missing or ambiguous: {target}')
            _check_control(page, matches[0])
    elif kind == 'click_button':
        if target:
            button = _find_field(scope, target, 'button, a, [role=button]')
        else:
            exact_buttons = scope.get_by_role('button', name=value, exact=True)
            exact_links = scope.get_by_role('link', name=value, exact=True)
            button = exact_buttons
            if exact_buttons.count() != 1:
                button = exact_links
            if button.count() > 1:
                hrefs = {href for href in button.evaluate_all(
                    "els => els.map(el => el.href || '')"
                ) if href}
                markup = set(button.evaluate_all("els => els.map(el => el.outerHTML)"))
                if len(hrefs) == 1 or len(markup) == 1:
                    button = button.first
            if button.count() != 1:
                button = scope.get_by_role('button', name=value, exact=False)
            if button.count() != 1:
                raise ValueError(f'Button missing or ambiguous: {value}')
        label = (button.inner_text() or button.get_attribute('value') or '').strip().lower()
        cookie = any(word in label for word in ('cookie', 'accept', 'accepteer', 'accepteren', 'consent', 'reject', 'weigeren', 'decline'))
        navigation = label in ('next', 'continue', 'volgende', 'verder', 'suivant', 'weiter')
        final_label = any(word in label for word in ('submit application', 'submit your application', 'send application', 'send your application', 'sollicitatie versturen', 'sollicitatie verzenden', 'verstuur sollicitatie', 'bewerbung absenden')) or label in ('submit', 'verzenden', 'versturen', 'absenden')
        application_form = button.evaluate('''el => !!el.form && (el.type === "submit") &&
            !!el.form.querySelector('input[type=file], input[type=email], input[name*=name i], input[name*=naam i], textarea')''')
        submit = not cookie and not navigation and (final_label or application_form)
        if not button.is_visible() or not button.is_enabled():
            raise ValueError("Button is hidden or disabled")
        if submit:
            page.context._external_receipts_before = visible_receipts(page)
            page.context._external_submit_attempted = True
        if getattr(page.context, "_external_backend", "edge") == "lightpanda":
            # Some ATS pages use a semantic button nested inside an anchor.
            # Calling click() only on the inner node is not enough in every
            # Chromium/CDP backend to trigger the anchor navigation.  Prefer
            # the nearest real link when present, while retaining the direct
            # button path for JS-only controls.
            current_url = page.url
            destination = button.evaluate("""el => {
                const link = el.closest('a[href]');
                return link ? link.href : '';
            }""")
            button.evaluate("""el => {
                const link = el.closest('a[href]');
                const target = link || el;
                if (target.tagName === 'A') target.target = '_self';
                target.click();
            }""")
            # CDP-backed browsers can report the old URL briefly after the
            # click even though the destination request has already started.
            # Wait for the navigation to commit so callers do not inspect the
            # old LinkedIn/ATS page and falsely mark the click as failed.
            if destination and destination != current_url:
                try:
                    page.wait_for_url(destination, timeout=3000, wait_until='domcontentloaded')
                except Exception:
                    # JS-only links may intentionally keep the same URL; the
                    # form loop will inspect the resulting page state.
                    pass
        else:
            button.click()
    elif kind == 'react_fill':
        editors = [field for field in scope.locator('[contenteditable=true]').all()
                   if target and target in (field.get_attribute('class') or '')]
        field = editors[0] if len(editors) == 1 else _find_field(scope, target, 'input, textarea, [contenteditable=true]')
        _fill_control(page, field, value)
    elif kind == 'fill_combobox':
        field = _find_field(scope, target, 'input[role=combobox]')
        if field.evaluate("el => el.tagName") == 'BUTTON':
            visible_value = ' '.join(filter(None, [field.inner_text(), field.get_attribute('aria-label')])).lower()
            if value.lower() in visible_value:
                # A number of phone widgets expose the country picker as a
                # button. If it already displays the requested country, it is
                # complete and must not be passed to Locator.fill().
                return
            raise ValueError('Combobox is a button with a different current value; use a click/select action')
        field.fill(value)
        field.press('ArrowDown')
        field.press('Enter')
    elif kind == 'press_key':
        if value.lower() in ('enter', 'return') and scope.evaluate('''() => {
            const el = document.activeElement;
            return !!el && el.tagName === 'INPUT' && el.getAttribute('role') !== 'combobox' &&
                !!el.form && !!el.form.querySelector('input[type=file], input[type=email], input[name*=name i], input[name*=naam i], textarea');
        }'''):
            page.context._external_receipts_before = visible_receipts(page)
            page.context._external_submit_attempted = True
        page.keyboard.press(value)
    else:
        raise ValueError(f'Unsupported external action: {kind}')
    page.wait_for_timeout(200)


def frame_state(page):
    lines = ['EXTERNAL BROWSER: ' + getattr(page.context, '_external_backend', 'edge'),
             'Frame targets: use frame[N]::field_name (or frame[N]::css=SELECTOR) for embedded forms.',
             'Use fill_input for textarea as well; do not repeat a final submit action.']
    for index, frame in enumerate(page.frames):
        if index == 0:
            continue
        lines.append(f'FRAME[{index}] URL: {frame.url[:200]}')
        try:
            lines.append(frame.locator('body').inner_text(timeout=3000)[:1500])
            for element in frame.locator('input, textarea, select, button').all()[:35]:
                lines.append(f'frame[{index}]::' + element.evaluate('el => el.name || el.id || el.outerHTML.slice(0, 200)'))
        except Exception as error:
            lines.append(f'Frame unavailable: {type(error).__name__}')
    return '\n'.join(lines)
