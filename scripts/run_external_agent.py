#!/usr/bin/env python3
"""Run one persistent Codex agent for an external regression case."""
import argparse,hashlib,json,locale,os,re,subprocess,sys,time,threading,urllib.request
from shutil import which
from pathlib import Path
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.config import load_config,get_resume_path
from src.storage import is_already_applied,save_for_manual_apply
from src.external_handoff_queue import mark_handoff_submitted, save_external_handoff, notify_handoff_required
from src.tracker import log_application
from src.external_browser import focus_external_browser
CODEX_MODEL=''
EDGE_ENDPOINT='http://127.0.0.1:9227'
EDGE_PATH=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
EDGE_PROFILE=ROOT/'data'/'external_edge_profile'
EDGE_PROCESS_PID=None
HANDOFF_RECEIPT_PHRASES=(
 'application received','application submitted','thank you for applying',
 'thanks for applying','your application has been sent','we received your application',
 'application has been successfully submitted','your application has been successfully submitted',
 'sollicitatie ontvangen','bedankt voor je sollicitatie',
)

def _discover_existing_edge_pid():
 """Find the Edge process that owns the configured CDP endpoint when possible."""
 try:
  raw=subprocess.check_output(['netstat','-ano','-p','tcp'],text=True,stderr=subprocess.DEVNULL,timeout=3)
  for line in raw.splitlines():
   fields=line.split()
   if len(fields)>=5 and fields[0].casefold()=='tcp' and fields[1].endswith(':9227') and fields[3].casefold()=='listening':
    return int(fields[4])
 except Exception:
  pass
 try:
  # Avoid depending on psutil: use the visible Edge window itself as the
  # foreground target when CDP was started by an existing browser session.
  import ctypes
  user32=ctypes.windll.user32
  found=[]
  callback_type=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
  def callback(hwnd,_):
   if not user32.IsWindowVisible(hwnd): return True
   length=user32.GetWindowTextLengthW(hwnd)
   if length<=0: return True
   title=ctypes.create_unicode_buffer(length+1);user32.GetWindowTextW(hwnd,title,length+1)
   text=title.value.casefold()
   if 'microsoft edge' not in text and 'edge' not in text: return True
   pid=ctypes.c_ulong();user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
   if pid.value: found.append(int(pid.value))
   return True
  user32.EnumWindows(callback_type(callback),0)
  if found: return found[0]
 except Exception:
  pass
 try:
  import psutil
  candidates=[]
  for process in psutil.process_iter(['pid','name','cmdline']):
   try:
    if str(process.info.get('name') or '').casefold()!='msedge.exe': continue
    command=' '.join(process.info.get('cmdline') or []).casefold()
    score=(2 if 'remote-debugging-port=9227' in command else 0)
    candidates.append((score,int(process.info['pid'])))
   except (psutil.Error,KeyError,TypeError,ValueError):
    continue
  if candidates: return max(candidates)[1]
 except Exception:
  pass
 try:
  # CDP is bound to this port; netstat identifies its owning Edge process
  # even when the title is hidden or contains locale-specific characters.
  raw=subprocess.check_output(['netstat','-ano','-p','tcp'],text=True,stderr=subprocess.DEVNULL,timeout=3)
  for line in raw.splitlines():
   fields=line.split()
   if len(fields)>=5 and fields[0].casefold()=='tcp' and fields[1].endswith(':9227') and fields[3].casefold()=='listening':
    return int(fields[4])
 except Exception:
  pass
 return None

def ensure_edge_cdp():
 global EDGE_PROCESS_PID
 def cdp_ready():
  try:
   with urllib.request.urlopen(EDGE_ENDPOINT+'/json/version',timeout=1): return True
  except Exception: return False
 if cdp_ready():
  # An already-running Edge instance is common. Preserve its PID so the
  # manual-handoff path can reliably bring the correct window to the front.
  EDGE_PROCESS_PID=_discover_existing_edge_pid()
  print(f'[external-agent] Existing Edge CDP detected (window_pid={EDGE_PROCESS_PID})',flush=True)
  return True
 if not os.path.isfile(EDGE_PATH):
  print('[external-agent] Edge fallback unavailable: msedge.exe not found',flush=True); return False
 profile=EDGE_PROFILE.parent/f'{EDGE_PROFILE.name}_{os.getpid()}_{int(time.time())}'
 profile.mkdir(parents=True,exist_ok=True)
 try:
  print(f'[external-agent] Starting Edge fallback on {EDGE_ENDPOINT} (profile={profile})',flush=True)
  # Edge 155 exits its CDP process when started with only about:blank; keep a
  # harmless real tab alive until the agent connects and navigates to the ATS.
  flags=getattr(subprocess,'DETACHED_PROCESS',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)
  process=subprocess.Popen([EDGE_PATH,'--remote-debugging-port=9227','--remote-debugging-address=127.0.0.1',f'--user-data-dir={profile}','--no-first-run','--no-default-browser-check','https://example.com'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
  EDGE_PROCESS_PID=process.pid
  for _ in range(20):
   time.sleep(.5)
   if cdp_ready():
    # Require a second stable probe before giving the MCP server this endpoint.
    time.sleep(.5)
    if cdp_ready(): return True
 except Exception as error:
  print(f'[external-agent] Edge fallback start failed ({profile.name}): {type(error).__name__}',flush=True)
 print('[external-agent] Edge fallback did not become ready',flush=True); return False
CASES=ROOT/'data'/'external_regression_cases.json';LOCKS=ROOT/'data'/'external_regression_pending.json'

def safe_run_key(value):
 """Return a Windows-safe directory component without changing queue keys."""
 cleaned=re.sub(r'[^A-Za-z0-9._-]+','_',str(value or '')).strip(' .')
 return (cleaned or 'external_case')[:120]

def build_case(args, cases):
 """Resolve a named regression case or one queued external URL."""
 if args.url:
  digest=hashlib.sha1(args.url.encode('utf-8')).hexdigest()[:12]
  return {
   'key':args.queue_key or f'url_{digest}',
   'title':args.title or 'Queued external application',
   'company':args.company or 'Unknown',
   'job_id':args.job_id or digest,
   'score':args.score,
   'url':args.url,
   'cover_letter_path':args.cover_letter or '',
   'allow_repeat_testing':True,
  }
 if not args.case or args.case not in cases:
  raise SystemExit('Unknown regression case; provide a configured case or --url')
 return cases[args.case]

def resolve_codex_executable():
 """Resolve the configured Codex binary, tolerating stale versioned paths."""
 runtime_path=ROOT/'codex_runtime.json'
 configured=''
 try:
  configured=str(json.loads(runtime_path.read_text(encoding='utf-8')).get('executable','')).strip()
 except (OSError,ValueError,TypeError):
  pass
 if configured and Path(configured).is_file():
  return configured
 roots=[]
 local_appdata=os.environ.get('LOCALAPPDATA','')
 if local_appdata:
  roots.append(Path(local_appdata)/'OpenAI'/'Codex'/'bin')
 roots.append(Path.home()/ 'AppData' / 'Local' / 'OpenAI' / 'Codex' / 'bin')
 candidates=[]
 for root in roots:
  try:
   candidates.extend(p for p in root.glob('*/codex.exe') if p.is_file())
  except OSError:
   continue
 if candidates:
  return str(max(candidates,key=lambda p:p.stat().st_mtime))
 discovered=which('codex.exe') or which('codex')
 if discovered and Path(discovered).suffix.casefold()=='.exe':
  return discovered
 raise FileNotFoundError(
  'Configured Codex executable is missing and no installed Codex binary was found; '
  f'configured={configured or "<none>"}'
 )
def run_streamed(command,env,out,timeout):
 """Mirror Codex output verbatim while showing compact, value-free tool events."""
 stop=threading.Event(); trace=out.parent/'diagnostics.jsonl'
 print(f'[external-agent] Run directory: {out.parent}',flush=True)
 with out.open('w',encoding='utf-8') as log:
  process=subprocess.Popen(command,cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,bufsize=0)
  def relay():
   for raw in process.stdout:
    try: line=raw.decode('utf-8')
    except UnicodeDecodeError: line=raw.decode(locale.getpreferredencoding(False),errors='replace')
    log.write(line);log.flush()
    try: print(line,end='',flush=True)
    except UnicodeEncodeError: print(line.encode(sys.stdout.encoding or 'utf-8',errors='replace').decode(sys.stdout.encoding or 'utf-8'),end='',flush=True)
  def progress():
   offset=0
   challenge_focused=False
   while not stop.is_set():
    if trace.exists():
     with trace.open('r',encoding='utf-8') as f:
      f.seek(offset)
      for line in f:
       try:
        item=json.loads(line);kind=item.get('kind')
        if kind in ('open','inspect','fill','checked','resume_upload','diagnose_validation','submission_diagnostics','challenge_recheck','submit_click_fallback','submit_click_error','submit_final','action_failed','page_error','page_crash','page_close','navigation','page_load','domcontentloaded','fallback_recommended','browser_switch','handoff_started','handoff_snapshot','handoff_receipt','handoff_timeout','handoff_focus_error','handoff_observation_error','handoff_page_unavailable'):
         if kind=='submission_diagnostics':
          runtime=item.get('runtime') or {}; captcha=item.get('captcha') or {}; flow=captcha.get('flow') or {}; print(f'[browser {item.get("time",0):.1f}s] diagnostics stage={item.get("stage")} valid={item.get("form_valid")} invalid={len(item.get("invalid_fields") or [])} challenges={len(item.get("challenges") or [])} captcha={flow.get("loaded")} token={flow.get("response_present")} busy={next((x.get("busy") for x in item.get("submitters",[]) if str(x.get("text","")).casefold()=="send"),"")} code={item.get("failure_code")} webdriver={runtime.get("webdriver")}',flush=True); continue
         if kind=='challenge_recheck':
          stable=int(item.get('stable',0) or 0)
          print(f'[browser {item.get("time",0):.1f}s] challenge_recheck stage={item.get("stage")} initial={item.get("initial")} stable={stable}',flush=True)
          if stable and not challenge_focused:
           # Do not foreground an arbitrary Edge window here. The dedicated
           # handoff watcher selects the retained application tab first and
           # then focuses Edge immediately before sounding the alert.
           print('[external-agent] Interactive challenge detected; deferring Edge focus until handoff watcher',flush=True)
           challenge_focused=True
          continue
         if kind=='handoff_started':
          print(f'[handoff] started timeout={item.get("timeout_seconds")} focused={item.get("focused")} url={item.get("url")}',flush=True)
          continue
         if kind=='handoff_snapshot':
          print(f'[handoff] page={item.get("url")} body_chars={item.get("body_chars")} visible_challenges={item.get("visible_challenges")} visible_widgets={item.get("visible_widgets")} response_token={item.get("response_present")} send_busy={item.get("send_busy")}',flush=True)
          continue
         if kind=='handoff_receipt':
          print(f'[handoff] employer receipt observed: {item.get("receipt")}',flush=True)
          continue
         if kind=='handoff_timeout':
          print(f'[handoff] timed out after {item.get("timeout_seconds")} seconds without an employer receipt',flush=True)
          continue
         if kind in ('handoff_focus_error','handoff_observation_error','handoff_page_unavailable'):
          print(f'[handoff] {kind} {item.get("error","")}',flush=True)
          continue
         details=' '.join(f'{k}={item[k]}' for k in ('backend','from_backend','to_backend','selector','retained','valid','checked','attempt','method','reason','failure_code','errors','invalid','network','receipt','error') if k in item)
         print(f'[browser {item.get("time",0):.1f}s] {kind} {details}',flush=True)
       except (ValueError,TypeError): pass
      offset=f.tell()
    stop.wait(.4)
  reader=threading.Thread(target=relay,daemon=True);watcher=threading.Thread(target=progress,daemon=True)
  reader.start();watcher.start()
  try: process.wait(timeout=timeout)
  except subprocess.TimeoutExpired:
   process.kill();process.wait()
   print('[external-agent] Codex timed out; submission status may be unknown.',flush=True)
  finally:
   reader.join(timeout=5);stop.set();watcher.join(timeout=2)
  return process.returncode
def recover_result_from_trace(run,result,url):
 """Persist a safe unconfirmed result if the agent stopped without calling finish()."""
 trace=run/'diagnostics.jsonl'
 if not trace.exists(): return None
 events=[]
 try:
  for line in trace.read_text(encoding='utf-8').splitlines():
   try: events.append(json.loads(line))
   except ValueError: continue
 except OSError: return None
 if not events: return None
 has_submit=any(e.get('kind')=='submit_final' for e in events)
 has_challenge=any(e.get('kind')=='challenge_recheck' and int(e.get('stable',0) or 0)>0 for e in events)
 if not (has_submit and has_challenge): return None
 post_submit=next((e for e in reversed(events) if e.get('kind')=='submission_diagnostics' and e.get('stage')=='after'),{})
 code=post_submit.get('failure_code') or 'visible_challenge'
 reason={
  'captcha_token_missing_visible_challenge':'hCaptcha did not return a token and a visible interactive challenge remained after Send; no employer receipt was observed',
  'captcha_token_missing_after_submit':'hCaptcha did not return a token after Send; no employer receipt was observed',
  'visible_challenge':'A visible interactive challenge remained after Send; no employer receipt was observed',
 }.get(code,'Agent stopped after a visible interactive challenge without calling finish; no employer receipt was observed')
 data={'status':'unconfirmed','reason':reason,'failure_code':code,'receipt':'','url':url,'backend':'edge','handoff_required':True,'challenges':[],'events':events}
 result.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
 return data
def preflight_blocking_reason(url):
 """Cheap, read-only Lightpanda preflight for actual login requirements.

 A hidden/invisible CAPTCHA integration is common and may generate its token
 automatically in a normal browser. It is metadata, not a pre-submit block.
 """
 try:
  from playwright.sync_api import sync_playwright
  settings=json.loads((ROOT/'external_browser.json').read_text(encoding='utf-8-sig'))
  with sync_playwright() as pw:
   browser=pw.chromium.connect_over_cdp(settings['endpoint'],timeout=10000)
   context=browser.new_context()
   try:
    page=context.new_page();page.goto(url,wait_until='domcontentloaded',timeout=45000)
    # Recruitee and similar boards show the actual application fields only
    # after this entry button. It is navigation, not a submission.
    buttons=[item for item in page.locator('button,a,[role=button]').all()
             if item.is_visible() and item.is_enabled() and (item.inner_text() or '').strip().casefold()=='apply']
    if buttons:
     max(buttons,key=lambda item:item.evaluate('e=>{const r=e.getBoundingClientRect();return r.width*r.height}')).evaluate('e=>e.click()')
     page.wait_for_timeout(700)
    if any(item.is_visible() for item in page.locator('input[type=password]').all()): return 'authentication_required'
   finally:
    context.close();browser.close()
 except Exception:
  # Preflight must never block a site merely because its browser is briefly unavailable.
  return ''
 return ''

def _handoff_receipt(body):
 low=' '.join((body or '').casefold().split())
 return next((phrase for phrase in HANDOFF_RECEIPT_PHRASES if phrase in low),'')

def wait_for_manual_handoff(run,result,data,case,timeout):
 """Keep the stable Edge page alive while a human handles a CAPTCHA/Send handoff.

 This deliberately performs observation only: it never clicks, refreshes, or
 retries.  A receipt is accepted only when it is visible in the employer page.
 """
 try:
  from playwright.sync_api import sync_playwright
 except Exception as error:
  print(f'[external-agent] Cannot start handoff watcher: {type(error).__name__}',flush=True)
  return 1
 deadline=time.time()+max(1,int(timeout))
 last_state=None
 print(f'[external-agent] Human handoff required; waiting up to {int(timeout)} seconds.',flush=True)
 print('[external-agent] Edge will remain on the current application page. Complete the visible challenge or click Send once; do not refresh.',flush=True)
 try:
  with sync_playwright() as pw:
   browser=pw.chromium.connect_over_cdp(EDGE_ENDPOINT,timeout=10000)
   pages=[]
   for context in browser.contexts:
    pages.extend(context.pages)
   page=next((item for item in reversed(pages) if urlsplit(item.url).hostname==urlsplit(case['url']).hostname),None)
   if page is None and pages: page=pages[-1]
   if page is None:
    print('[external-agent] Handoff watcher found no Edge page; browser state was not changed.',flush=True)
    return 1
   try: page.bring_to_front()
   except Exception: pass
   focused=focus_external_browser(page=page,process_id=EDGE_PROCESS_PID)
   try: page.bring_to_front()
   except Exception: pass
   # Emit the alert only after the retained application tab has been selected
   # and Edge has been foregrounded.  This prevents a LinkedIn detail tab from
   # being shown for the handoff.
   notify_handoff_required(source="external_agent_ready")
   print(f'[external-agent] Edge window focus requested (focused={focused})',flush=True)
   while time.time()<deadline:
    try:
     body=page.locator('body').inner_text(timeout=3000)
     receipt=_handoff_receipt(body)
     if receipt:
      updated=dict(data);updated.update({'status':'submitted','reason':f'Employer receipt observed during manual handoff: {receipt}','receipt':receipt,'url':page.url,'backend':'edge','handoff_required':False})
      result.write_text(json.dumps(updated,ensure_ascii=False,indent=2),encoding='utf-8')
      if not is_already_applied(case['job_id']):
       # The runner may be the component that writes the ledger (the parent
       # process deliberately avoids a duplicate row). Preserve the actual
       # file role used on the successful attempt.
       cover_status = 'yes' if str(case.get('cover_letter_path') or a.cover_letter or '').strip() else 'not applicable'
       log_application(job_id=case['job_id'],company=case['company'],title=case['title'],match_score=case['score'],apply_method='external',cover_letter_status=cover_status)
      print(f'[external-agent] Employer receipt observed during handoff: {receipt}',flush=True)
      return 0
     frames=page.locator('iframe[src*="captcha" i]')
     visible=0
     for index in range(frames.count()):
      try:
       if frames.nth(index).is_visible(): visible+=1
      except Exception: pass
     state=(page.url,len(body),page.locator('form').count(),visible)
     if state!=last_state:
      print(f'[handoff] url={page.url} body_chars={len(body)} forms={state[2]} visible_captcha={visible} receipt=no',flush=True)
      last_state=state
     time.sleep(5)
    except Exception as error:
     print(f'[handoff] observation_error={type(error).__name__}',flush=True)
     time.sleep(5)
   updated=dict(data);updated.update({'status':'unconfirmed','reason':f'Manual handoff timed out after {int(timeout)} seconds; no employer receipt was observed','url':page.url,'backend':'edge','handoff_required':True})
   result.write_text(json.dumps(updated,ensure_ascii=False,indent=2),encoding='utf-8')
   print('[external-agent] Handoff timed out without an employer receipt.',flush=True)
   return 1
 except Exception as error:
  print(f'[external-agent] Handoff watcher stopped: {type(error).__name__}',flush=True)
  return 1
def main():
 ap=argparse.ArgumentParser();ap.add_argument('case',nargs='?',help='Configured regression case key');ap.add_argument('--url',default='',help='Queued external URL (skips external search)');ap.add_argument('--title',default='');ap.add_argument('--company',default='');ap.add_argument('--job-id',default='');ap.add_argument('--score',type=float,default=0);ap.add_argument('--queue-key',default='');ap.add_argument('--cover-letter',default='',help='Job-specific DOCX generated by the cover-letter session');ap.add_argument('--clear-verified',action='store_true');ap.add_argument('--timeout',type=int,default=300);ap.add_argument('--handoff-timeout',type=int,default=120,help='Seconds to keep Edge open for a human CAPTCHA/Send handoff.');ap.add_argument('--defer-handoff',action='store_true',help='Retain CAPTCHA pages and exit immediately so another application can run.');ap.add_argument('--preflight',action='store_true',help='Run the optional read-only login preflight; it opens the URL once before the agent.') ;a=ap.parse_args()
 cases={x['key']:x for x in json.loads(CASES.read_text(encoding='utf-8'))};case=build_case(a,cases);case_key=case['key']
 pending=json.loads(LOCKS.read_text(encoding='utf-8')) if LOCKS.exists() else {}
 if a.clear_verified:
  pending.pop(case_key,None);LOCKS.write_text(json.dumps(pending,indent=2),encoding='utf-8');print('Lock cleared after user verification.');return
 repeat_test=bool(case.get('allow_repeat_testing'))
 if case_key in pending and not repeat_test:raise SystemExit('Unresolved prior attempt: check employer receipt, then use --clear-verified.')
 if is_already_applied(case['job_id']) and not repeat_test:raise SystemExit('Already recorded as submitted; refusing duplicate.')
 config=load_config(None);resume=get_resume_path(config).resolve();host=urlsplit(case['url']).hostname
 block=preflight_blocking_reason(case['url']) if a.preflight else ''
 if not a.preflight:
  print('[external-agent] Optional preflight skipped; the agent will inspect the single stable Edge page.',flush=True)
 if block:
  save_for_manual_apply(case,block,case['url'],score=case['score'])
  print(f'External preflight blocked: {block}. Added to manual-apply queue; Codex was not started.')
  return 1
 run=ROOT/'data'/'external_agent_runs'/f"{safe_run_key(case_key)}_{time.strftime('%Y%m%d_%H%M%S')}";run.mkdir(parents=True);(run/'case_key.txt').write_text(case_key,encoding='utf-8');result=run/'result.json'
 if not repeat_test:
  pending[case_key]={'url':case['url'],'state':'agent_attempt_started','run':str(run)};LOCKS.write_text(json.dumps(pending,indent=2),encoding='utf-8')
 env=os.environ.copy()
 if not env.get('HOME'):
  env['HOME']=env.get('USERPROFILE') or ((env.get('HOMEDRIVE') or '')+(env.get('HOMEPATH') or ''))
 if not env.get('CODEX_HOME') and env.get('HOME'):
  env['CODEX_HOME']=str(Path(env['HOME'])/'.codex')
 env.update({'EXTERNAL_AGENT_RESUME':str(resume),'EXTERNAL_AGENT_COVER_LETTER':str(case.get('cover_letter_path') or a.cover_letter or ''),'EXTERNAL_AGENT_RESULT':str(result),'EXTERNAL_AGENT_ALLOWED_HOST':host,'EXTERNAL_AGENT_DEADLINE_SECONDS':str(a.timeout),'EXTERNAL_AGENT_HANDOFF_TIMEOUT_SECONDS':str(a.handoff_timeout),'EXTERNAL_AGENT_DEFER_HANDOFF':'1' if a.defer_handoff else '0'})
 edge_ready=ensure_edge_cdp()
 print(f'[external-agent] Edge CDP default: {"ready" if edge_ready else "unavailable"}',flush=True)
 if not edge_ready:
  print('[external-agent] Edge is the configured default external browser; stopping before any application data is entered.',flush=True)
  return 1
 try:
  codex=resolve_codex_executable()
 except FileNotFoundError as error:
  print(f'[external-agent] {error}',flush=True)
  return 1
 server=ROOT/'src'/'external_agent_mcp.py'
 mcp_env={'EXTERNAL_AGENT_RESUME':str(resume),'EXTERNAL_AGENT_COVER_LETTER':str(case.get('cover_letter_path') or a.cover_letter or ''),'EXTERNAL_AGENT_RESULT':str(result),'EXTERNAL_AGENT_ALLOWED_HOST':host,'EXTERNAL_AGENT_DEADLINE_SECONDS':str(a.timeout),'EXTERNAL_AGENT_HANDOFF_TIMEOUT_SECONDS':str(a.handoff_timeout),'EXTERNAL_AGENT_DEFER_HANDOFF':'1' if a.defer_handoff else '0','EXTERNAL_AGENT_CDP_ENDPOINT':EDGE_ENDPOINT,'EXTERNAL_AGENT_INITIAL_BACKEND':'edge'}
 env_toml='{'+','.join(key+'='+json.dumps(value) for key,value in mcp_env.items())+'}'
 cfg='mcp_servers.external_apply_agent={command='+json.dumps(sys.executable)+',args=['+json.dumps(str(server))+'],env='+env_toml+',default_tools_approval_mode="approve"}'
 prompt=f'''Use only external_apply_agent MCP tools for this application. Your first tool call must be open_application({case['url']}); do not call inspect before a page is open. Then inspect before entering data. Build a required-field checklist from metadata.required_fields and metadata.missing_required_fields and keep working until every visible required field is valid and retained. When a page contains multiple forms, treat metadata.application_form (normally #job-application-form or the form containing candidate/file fields) as the only application schema; ignore unrelated auto-join, newsletter, account-login, or domain-login forms and do not fill their fields just to make global form_valid true. Use metadata.visible_widget_options to identify the currently open React, phone-country, or location option list. Passive hidden challenge integrations alone do not block. A challenge signal is not a reason to abandon incomplete form fields: fill all non-challenge required fields first, then hand off the completed form if a visible interactive challenge remains. Use one stable Edge page: do not refresh, open duplicate tabs, or reopen the URL unless submit_final explicitly returns fallback_recommended=true. Let dynamic controls settle before each action and avoid speculative selectors. If a custom country, phone-country, or React dropdown is required, use select_custom_option with the inspected trigger and exact visible option text; do not guess hidden li or role selectors. React-Select inputs normally become blank after a successful choice; trust a successful select_custom_option result and confirm with inspect.required_fields rather than retrying the same option. Use press_key only for an inspected editable combobox when the option list is searchable. For a single free-form phone field use candidate_context.phone_international when available; for a separate country selector use the national phone value and select the inspected country. If the country widget still cannot retain a selection after one normal option attempt, fill the phone with candidate_context.phone_international as a fallback and revalidate; never leave a phone error unresolved. If upload_resume fails or the file input detaches, inspect again and call upload_resume once more with the current inspected file selector; a successful employer upload response may replace the native file input, so accept the retained resume widget when diagnostics reports the upload network confirmation. If a required file field is clearly labeled Cover Letter, Motivation Letter, or equivalent, never upload the resume there. If EXTERNAL_AGENT_COVER_LETTER is configured, call upload_cover_letter with the inspected cover-letter selector and verify the retained filename; if it is not configured, call finish(reason="cover_letter_required") before any submission so the parent can generate a job-specific DOCX through the dedicated cover-letter session. Do not proceed while any required file field is missing. If the same specific action fails repeatedly, stop that action and call diagnose_validation with the evidence; do not loop through guessed selectors. Use click_text("Apply") only to enter the form. Call candidate_context once for all visible questions per form state, with open_ended=true for open questions. Use only truthful answers and the configured resume. Before any CAPTCHA handoff or submission call diagnose_validation and resolve every missing_required_fields item first. Before submit_final, inspect for visible cookie banners, auth/login dialogs, Pusher overlays, or other modal layers that could intercept the application submit control; dismiss a clearly unrelated overlay with its exact visible control and re-inspect. Use submit_final() for final submission, never click or click_text. If submit_final reports click_interceptor, do not guess: inspect the reported top element/dialogs, dismiss the exact unrelated overlay if present, then revalidate and retry once. Submit only after all application-form controls are valid and settled. The user explicitly authorizes up to two retries (three submit attempts total) only when no challenge is present and a correctable form/runtime failure is evidenced. After each attempt, wait up to 10 seconds and inspect for a visible receipt; if found call finish immediately. If no receipt, diagnose validation and fix any correctable errors. Treat a visible_challenge or captcha_token_missing_visible_challenge result as a CAPTCHA handoff only after missing_required_fields is empty: call finish(reason=that failure code plus the evidence) immediately and do not click, refresh, reopen, or retry the CAPTCHA. Treat captcha_token_missing_after_submit as a non-visible CAPTCHA/runtime stall: call finish with the evidence, but do not tell the user a visible challenge is present. If submit_final returns fallback_recommended=true, immediately call open_application with the original URL and continue on the fresh page; this is the supported recovery for a Lightpanda hCaptcha runtime error or a valid form whose Send control stays busy with no employer request. Do not switch browsers for any other error. On a fresh page, re-inspect, reuse known truthful answers, refill and upload the same resume, and attempt submit_final again. Respect the tool's maximum of three attempts. Do not retry CAPTCHA or authentication blocks. If a visible challenge appears after the form is complete, call finish(reason=...) before your final response so the run always writes result.json. If still unconfirmed after retries, call finish with evidence; never claim success without a visible employer receipt. Do not navigate away from {host}.'''
 out=run/'codex_stdout.log'
 # Keep the configured MCP server and mirror the model's own progress messages.
 print(f'[external-agent] Codex executable: {codex}',flush=True)
 model=str((config.get('ai') or {}).get('model') or CODEX_MODEL or '').strip()
 command=[codex,'--ask-for-approval','never']
 if model: command.extend(['--model',model])
 command.extend(['-c',cfg,'exec','-C',str(ROOT),'-s','workspace-write','--skip-git-repo-check','--enable','skip_host_skill_discovery','--ephemeral',prompt])
 print(f'[external-agent] Codex model: {model or "local default"}',flush=True)
 run_streamed(command,env,out,a.timeout+90)
 if not result.exists():
  recovered=recover_result_from_trace(run,result,case['url'])
  if recovered:
   if recovered.get('handoff_required'):
    save_external_handoff(case,recovered['reason'],case['url'],score=case['score'],source='external_agent',run_path=str(run),notify=False)
   else:
    save_for_manual_apply(case,recovered['reason'],case['url'],score=case['score'])
   print('Manual challenge handoff required; result recovered from diagnostics. Not confirmed:',recovered['reason'])
   return 1
 if not result.exists():
  output=out.read_text(encoding='utf-8',errors='replace').lower()
  if any(marker in output for marker in ('hit your usage limit','usage limit has been reached','rate limit reached')):
   if 'external_apply_agent/submit_final started' in output:
    print('Codex usage limit reached after a submission attempt. Submission status is UNKNOWN; check employer confirmation before retrying. See',out)
   else:
    print('Codex usage limit reached; no final submission attempt was observed. Check the reset time in the log. See',out)
  elif 'external_apply_agent' in output and 'mcp tools' in output and ('not exposed' in output or 'unavailable' in output):
   print('Agent runtime error: external_apply_agent MCP tools were unavailable; no application data was entered. See',out)
  else: print('Agent ended without a confirmed receipt; see',out)
  return 1
 data=json.loads(result.read_text(encoding='utf-8'))
 handoff_codes={'visible_challenge','captcha_token_missing_visible_challenge'}
 if not a.defer_handoff and not data.get('handoff_waited') and (data.get('handoff_required') or data.get('failure_code') in handoff_codes):
  handoff_status=wait_for_manual_handoff(run,result,data,case,a.handoff_timeout)
  data=json.loads(result.read_text(encoding='utf-8')) if result.exists() else data
  if handoff_status==0 and data.get('status')=='submitted':
   mark_handoff_submitted(case)
   if not repeat_test:
    pending[case_key]['state']='submitted';LOCKS.write_text(json.dumps(pending,indent=2),encoding='utf-8')
   print('Confirmed submitted and recorded after manual handoff.');return 0
 if data['status']=='submitted':
  mark_handoff_submitted(case)
  cover_status = 'yes' if str(case.get('cover_letter_path') or a.cover_letter or '').strip() else 'not applicable'
  log_application(job_id=case['job_id'],company=case['company'],title=case['title'],match_score=case['score'],apply_method='external',cover_letter_status=cover_status)
  if not repeat_test:
   pending[case_key]['state']='submitted';LOCKS.write_text(json.dumps(pending,indent=2),encoding='utf-8')
  print('Confirmed submitted and recorded.');return 0
 if data.get('handoff_required'):
  save_external_handoff(case,data['reason'],case['url'],score=case['score'],source='external_agent',run_path=str(run),notify=False)
 else:
  save_for_manual_apply(case,data['reason'],case['url'],score=case['score'])
 if data.get('handoff_required'):
  if a.defer_handoff:
   print('CAPTCHA page retained; queued for later manual completion. No wait performed. Not confirmed:',data['reason'])
  else:
   print('Manual challenge handoff required; browser state and diagnostics were retained. Not confirmed:',data['reason'])
 else:
  print('Not confirmed:',data['reason'])
 return 1
if __name__=='__main__':raise SystemExit(main())

