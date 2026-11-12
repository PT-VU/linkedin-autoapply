"""Bounded browser tools for one persistent external-application Codex session."""
import asyncio, json, os, re, time
from pathlib import Path
from urllib.parse import urlsplit
from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright
try:
    from external_browser import focus_external_browser
except Exception:
    focus_external_browser = None
try:
    from external_handoff_queue import notify_handoff_required
except Exception:
    notify_handoff_required = lambda: None

mcp=FastMCP("external_apply_agent")
pw=browser=context=page=None
initial_endpoint=os.environ.get("EXTERNAL_AGENT_CDP_ENDPOINT","http://127.0.0.1:9223").strip()
fallback_endpoint=os.environ.get("EXTERNAL_AGENT_FALLBACK_CDP_ENDPOINT","").strip()
current_endpoint=""
initial_backend=os.environ.get("EXTERNAL_AGENT_INITIAL_BACKEND","lightpanda").strip().lower() or "lightpanda"
active_backend=initial_backend
started=time.monotonic(); events=[]; network_events=[]; submission_attempts=0; baseline_receipts=set(); receipt_observed_after_submit=False; failed_action_counts={}; failed_action_total=0; resume_upload_confirmed=False; upload_confirmed_roles=set(); active_upload_role=""
deadline=int(os.environ.get("EXTERNAL_AGENT_DEADLINE_SECONDS","300"))
handoff_timeout=int(os.environ.get("EXTERNAL_AGENT_HANDOFF_TIMEOUT_SECONDS","120"))
defer_handoff=os.environ.get("EXTERNAL_AGENT_DEFER_HANDOFF","0").strip().casefold() in {"1","true","yes","on"}
allowed_host=os.environ.get("EXTERNAL_AGENT_ALLOWED_HOST","").lower()
result_path=Path(os.environ.get("EXTERNAL_AGENT_RESULT","external_agent_result.json"))

def guard():
    if time.monotonic()-started>deadline: raise RuntimeError("Five-minute external-agent deadline reached")
def event(kind,**data):
    entry={"time":round(time.monotonic()-started,3),"kind":kind,**data}
    events.append(entry)
    # Incremental local trace survives Codex quota exhaustion or a crash.
    result_path.parent.mkdir(parents=True,exist_ok=True)
    with (result_path.parent/"diagnostics.jsonl").open("a",encoding="utf-8") as f:
        f.write(json.dumps(entry,ensure_ascii=False,default=str)+"\n")

def action_failed(kind, key, error):
    """Record failed UI actions and stop an agent that is looping.

    A failed selector is diagnostic evidence, not a reason to keep clicking.
    The limits are per MCP process and do not touch the page or CAPTCHA.
    """
    global failed_action_total
    action_key=f"{kind}:{key}"
    failed_action_counts[action_key]=failed_action_counts.get(action_key,0)+1
    failed_action_total+=1
    count=failed_action_counts[action_key]
    event("action_failed",action=kind,key=str(key)[:240],count=count,total=failed_action_total,error=type(error).__name__,detail=str(error)[:800])
    if count>=3 or failed_action_total>=8:
        raise RuntimeError(f"Repeated external action failures ({kind}, count={count}, total={failed_action_total}); stop retrying and call finish")
def validate_url(url):
    host=(urlsplit(url).hostname or "").lower()
    if not host or (allowed_host and host!=allowed_host and not host.endswith("."+allowed_host)): raise ValueError("Navigation outside the approved host is blocked")
async def one(selector):
    target=page.locator(selector)
    count=await target.count()
    if count==1: return target
    # React ATS forms may leave an old hidden clone while replacing a control.
    # Prefer the single attached visible/enabled instance before declaring a
    # selector ambiguous; genuinely different visible controls still fail
    # closed and are reported to the agent.
    candidates=[]
    for index in range(count):
        candidate=target.nth(index)
        try:
            attached=await candidate.evaluate("e=>e.isConnected")
            if not attached: continue
            if await candidate.is_visible() and await candidate.is_enabled(): candidates.append(candidate)
        except Exception: continue
    if len(candidates)==1: return candidates[0]
    attached=[]
    for index in range(count):
        candidate=target.nth(index)
        try:
            if await candidate.evaluate("e=>e.isConnected"): attached.append(candidate)
        except Exception: continue
    if len(attached)==1: return attached[0]
    raise ValueError("Target must be unique: "+selector)
def network(kind,**data):
    # URL query strings can contain applicant data or tokens. Do not log them.
    if data.get("url"):
        url=urlsplit(data["url"]); data["url"]=url._replace(query="",fragment="").geturl()
    global resume_upload_confirmed
    if kind=="response" and str(data.get("url","")).casefold().find(".amazonaws.com")>=0 and int(data.get("status",0) or 0) in range(200,300):
        if active_upload_role:
            upload_confirmed_roles.add(active_upload_role)
        if active_upload_role == "resume":
            resume_upload_confirmed=True
        event("resume_upload_network_confirmed",status=data.get("status"),url=data.get("url"))
    entry={"time":round(time.monotonic()-started,3),"kind":kind,**data}
    network_events.append(entry); del network_events[:-100]
    event("network_"+kind,**data)
async def request_metadata(request):
    """Return request shape without persisting applicant values or file bytes."""
    content_type=''
    try: content_type=await request.header_value('content-type') or ''
    except Exception: pass
    post_data=''
    try: post_data=request.post_data or ''
    except Exception: pass
    keys=[]
    if post_data:
        try:
            parsed=json.loads(post_data)
            if isinstance(parsed,dict): keys=sorted(str(k) for k in parsed.keys())[:80]
        except Exception:
            for part in re.split(r'&|\r?\n',post_data):
                key=part.split('=',1)[0].strip()
                if key and key not in keys: keys.append(key[:120])
            keys=keys[:80]
    return {'content_type':content_type[:160],'post_data_length':len(post_data),'post_keys':keys}
def attach_network(target):
    async def on_request(request):
        if request.method.upper() not in {"POST","PUT","PATCH","DELETE"}: return
        try:
            metadata=await request_metadata(request)
            network("request",method=request.method,url=request.url[:500],**metadata)
        except Exception as error:
            # Diagnostics must never break the browser tool itself.
            event("network_metadata_error",method=request.method,error=type(error).__name__)
    async def on_response(response):
        if response.request.method.upper() in {"POST","PUT","PATCH","DELETE"}:
            network("response",method=response.request.method,status=response.status,url=response.url[:500])
    async def on_request_failed(request):
        if request.method.upper() in {"POST","PUT","PATCH","DELETE"}:
            network("request_failed",method=request.method,url=request.url[:500],error=request.failure)
    def on_page_error(error):
        event("page_error",error=str(error)[:1000],stack=str(getattr(error,"stack","") or "")[:4000])
    def on_console(message):
        if message.type in {"error","warning"}: event("console",type=message.type,text=message.text[:800])
    def on_frame_navigated(frame):
        # Navigation is the key signal for diagnosing a human handoff that
        # unexpectedly reloads or turns into an empty document. Keep the URL
        # query-free because ATS URLs can contain applicant data.
        if frame == target.main_frame:
            clean=urlsplit(frame.url)._replace(query="",fragment="").geturl()
            event("navigation",url=clean)
    def on_load():
        event("page_load",url=urlsplit(target.url)._replace(query="",fragment="").geturl())
    def on_domcontentloaded():
        event("domcontentloaded",url=urlsplit(target.url)._replace(query="",fragment="").geturl())
    def on_close():
        event("page_close",url=urlsplit(target.url)._replace(query="",fragment="").geturl())
    def on_crash():
        event("page_crash",url=urlsplit(target.url)._replace(query="",fragment="").geturl())
    target.on("request",on_request)
    target.on("response",on_response)
    target.on("requestfailed",on_request_failed)
    target.on("pageerror",on_page_error)
    target.on("console",on_console)
    target.on("framenavigated",on_frame_navigated)
    target.on("load",on_load)
    target.on("domcontentloaded",on_domcontentloaded)
    target.on("close",on_close)
    target.on("crash",on_crash)

def application_request_seen(items):
    """Return true only for a likely same-host application mutation.

    Analytics, CAPTCHA, Sentry and challenge traffic is deliberately excluded;
    a real employer mutation must suppress automatic browser fallback to avoid
    creating a duplicate application when the receipt is merely delayed.
    """
    telemetry=("sentry","analytics","captcha","hcaptcha","recaptcha","challenge","indeed","widget_params")
    for item in items:
        method=str(item.get("method","")).upper()
        if method not in {"POST","PUT","PATCH","DELETE"}: continue
        host=(urlsplit(str(item.get("url",""))).hostname or "").lower()
        if allowed_host and host not in {allowed_host} and not host.endswith("."+allowed_host): continue
        path=(urlsplit(str(item.get("url",""))).path or "").casefold()
        url_text=str(item.get("url","" )).casefold()
        if any(token in url_text or token in path for token in telemetry): continue
        return True
    return False

def hcaptcha_runtime_blocked(items, evidence=None):
    if active_backend != "lightpanda" or not fallback_endpoint or application_request_seen(items): return False
    for item in events:
        if item.get("kind")!="page_error": continue
        text=(str(item.get("error",""))+" "+str(item.get("stack",""))).casefold()
        if "no access" in text and "captcha" in text: return True
    # Lightpanda can fail silently: Recruitee sets the Send control busy while
    # hCaptcha is loaded, but no page error or application request is emitted.
    # Require all form-side signals together before switching to Edge so a
    # normal slow request or an unrelated validation issue is never replayed.
    if evidence:
        if evidence.get("errors") or evidence.get("invalid_fields") or evidence.get("challenges"):
            return False
        captcha=evidence.get("captcha") or {}
        busy=any(str(item.get("busy","")).casefold()=="true" for item in (evidence.get("submitters") or []))
        loaded=captcha.get("hcaptcha_type") in {"object","function"} or captcha.get("grecaptcha_type") in {"object","function"}
        if busy and loaded:
            return True
    return False
async def install_observers():
    await page.evaluate("""() => {if(window.__externalAgentSubmission)return;const s=window.__externalAgentSubmission={submits:[],clicks:[],errors:[],fetches:[]};
      document.addEventListener('submit',e=>s.submits.push({time:Date.now(),action:e.target?.action||'',method:e.target?.method||'',trusted:!!e.isTrusted,defaultPrevented:!!e.defaultPrevented}),true);
      document.addEventListener('click',e=>{const x=e.target?.closest?.('button,input[type=submit],[role=button]');if(x)s.clicks.push({time:Date.now(),text:(x.innerText||x.value||'').trim(),type:x.type||'',disabled:!!x.disabled,trusted:!!e.isTrusted,pointerType:e.pointerType||'',detail:e.detail||0})},true);
      window.addEventListener('error',e=>s.errors.push({message:String(e.message||e.error||'error'),stack:String(e.error?.stack||'').slice(0,4000)}));window.addEventListener('unhandledrejection',e=>s.errors.push({message:String(e.reason||'unhandled rejection'),stack:String(e.reason?.stack||'').slice(0,4000)}))}""")

async def runtime_metadata():
    """Return non-sensitive browser/runtime facts useful for comparing a human handoff.

    This deliberately records no cookies, storage, form values, or challenge tokens.
    """
    return await page.evaluate("""() => ({
      webdriver: navigator.webdriver,
      user_agent: navigator.userAgent,
      platform: navigator.platform,
      languages: navigator.languages,
      hardware_concurrency: navigator.hardwareConcurrency,
      max_touch_points: navigator.maxTouchPoints,
      viewport: {width: innerWidth, height: innerHeight, dpr: devicePixelRatio},
      visibility: document.visibilityState,
      has_focus: document.hasFocus(),
      secure_context: isSecureContext,
      cdp_runtime: !!window.chrome && !!window.chrome.runtime,
      permissions_query: !!navigator.permissions
    })""")

async def captcha_flow_metadata():
    """Read CAPTCHA state without executing, clicking, or exposing a token.

    Recruitee's React form can accept a trusted Send click and still never
    reach its employer API because its onSubmit handler waits for hCaptcha's
    onVerify callback.  The presence bit is enough to diagnose that gate;
    the token itself is deliberately never persisted.
    """
    return await page.evaluate("""() => {
      const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
        return !!(r.width>=30&&r.height>=30&&s.display!=='none'&&s.visibility!=='hidden')};
      const challengeNodes=[...document.querySelectorAll(
        'iframe[src*=captcha i],iframe[src*=recaptcha i],iframe[src*=hcaptcha i],iframe[src*=turnstile i],'+
        '[class*=captcha i],[id*=captcha i],[class*=turnstile i],[id*=turnstile i],[class*=hcaptcha i]')];
      const visibleChallenges=challengeNodes.filter(e=>{
        const marker=(e.id+' '+String(e.className||'')).toLowerCase();
        const errorNode=e.getAttribute('role')==='alert'||/(^|[-_ ])(error|invalid)([-_ ]|$)/.test(marker);
        const likelyWidget=e.tagName==='IFRAME'||/(^|[-_ ])(hcaptcha|recaptcha|turnstile)([-_ ]|$)/.test(marker);
        return likelyWidget&&!errorNode&&visible(e);
      }).length;
      let responsePresent=false, responseLength=0, getResponseAvailable=false, getResponseError='';
      try {
        getResponseAvailable=typeof window.hcaptcha?.getResponse==='function';
        if(getResponseAvailable){const response=window.hcaptcha.getResponse();
          responseLength=typeof response==='string'?response.length:0; responsePresent=responseLength>0;}
      } catch(e){getResponseError=String(e?.name||e).slice(0,120)}
      const form=document.querySelector('form');
      const key=form&&Object.keys(form).find(k=>k.startsWith('__reactProps$'));
      const props=key?form[key]:null;
      const handler=props&&typeof props.onSubmit==='function'?String(props.onSubmit):'';
      return {
        loaded: typeof window.hcaptcha==='object'||typeof window.hcaptcha==='function',
        execute_available: typeof window.hcaptcha?.execute==='function',
        get_response_available: getResponseAvailable,
        response_present: responsePresent,
        response_length: responseLength,
        get_response_error: getResponseError,
        visible_widgets: visibleChallenges,
        react_submit_handler: {
          present: !!handler,
          prevents_default: /preventDefault\\s*\\(/.test(handler),
          calls_captcha_execute: /(?:hcaptcha|captcha|z\\.current)\\s*[^;]{0,180}\\.execute\\s*\\(/i.test(handler),
          source_length: handler.length
        }
      };
    }""")

def classify_submission_state(evidence):
    """Return a stable, explainable failure code from a post-submit snapshot."""
    if not evidence:
        return "no_post_submit_evidence"
    if receipts(evidence.get("body", "")):
        return "receipt_visible"
    captcha=evidence.get("captcha") or {}
    submitters=evidence.get("submitters") or []
    busy=any(str(item.get("busy", "")).casefold()=="true" for item in submitters)
    employer_request=application_request_seen(evidence.get("network") or [])
    if evidence.get("invalid_fields") or evidence.get("missing_required_fields"):
        return "form_invalid"
    flow=captcha.get("flow") or {}
    if flow.get("loaded") and not flow.get("response_present") and busy:
        if evidence.get("challenges") or int(flow.get("visible_widgets", 0) or 0)>0:
            return "captcha_token_missing_visible_challenge"
        if flow.get("get_response_error"):
            return "captcha_token_read_error"
        return "captcha_token_missing_after_submit"
    if employer_request:
        return "employer_request_without_receipt"
    if evidence.get("challenges"):
        return "visible_challenge"
    return "no_receipt_no_employer_request"

async def snapshot(since=0):
    dom=await page.evaluate("""() => {const v=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return!!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden')};
      const errors=[...document.querySelectorAll('[role=alert],[aria-invalid=true],[class*=error i],[class*=invalid i]')].filter(v).map(e=>e.innerText.trim()).filter(Boolean);
      const challengeNodes=[...document.querySelectorAll('iframe[src*=captcha i],iframe[src*=recaptcha i],iframe[src*=hcaptcha i],iframe[src*=turnstile i],[class*=captcha i],[id*=captcha i],[class*=turnstile i],[id*=turnstile i],[class*=hcaptcha i]')];
      const challengeSignals=challengeNodes.map(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e),marker=(e.id+' '+String(e.className||'')).toLowerCase(),errorNode=e.getAttribute('role')==='alert'||/(^|[-_ ])(error|invalid)([-_ ]|$)/.test(marker),hasMeaningfulText=!!(e.innerText||'').trim(),interactive=!!(r.width>=30&&r.height>=30&&s.display!=='none'&&s.visibility!=='hidden'&&!errorNode&&(e.tagName==='IFRAME'||hasMeaningfulText||e.tabIndex>=0));return {tag:e.tagName,src:e.src||'',text:(e.innerText||'').trim().slice(0,200),title:e.title||'',interactive}});
      const allForms=[...document.forms];
      const applicationForm=allForms.find(f=>f.id==='job-application-form')||allForms
        .filter(f=>f.querySelector('input[type=file],input[name*="candidate" i],textarea[name*="candidate" i],[id*="candidate" i]'))
        .sort((a,b)=>Number(/application|apply|candidate/i.test(b.id+' '+b.name+' '+b.action))-Number(/application|apply|candidate/i.test(a.id+' '+a.name+' '+a.action)))[0]||null;
      const scope=applicationForm||document;
      const invalid=[...scope.querySelectorAll('input,select,textarea')].filter(e=>e.willValidate&&!e.validity.valid).map(e=>({id:e.id,name:e.name,type:e.type,message:e.validationMessage||''}));
      const submitters=[...document.querySelectorAll('button[type=submit],input[type=submit],form button,[role=button]')].filter(v).map(e=>({text:(e.innerText||e.value||'').trim(),type:e.type||'',disabled:!!e.disabled,busy:e.getAttribute('aria-busy')||'',formAction:e.formAction||''}));
      const reactInfo=e=>{const key=Object.keys(e||{}).find(k=>k.startsWith('__reactProps$'));const props=key?e[key]:null;return {has_react_props:!!props,handlers:props?Object.keys(props).filter(k=>/^on[A-Z]/.test(k)).sort():[]}};
      const activeForms=applicationForm?[applicationForm]:allForms;
      const forms=allForms.map(f=>({id:f.id||'',action:f.action||'',method:(f.method||'get').toUpperCase(),enctype:f.enctype||'',no_validate:!!f.noValidate,check_validity:f.checkValidity(),active_for_application:f===applicationForm,elements:f.elements.length,onsubmit:typeof f.onsubmit==='function',react:reactInfo(f),submit_controls:[...f.querySelectorAll('button[type=submit],input[type=submit]')].map(e=>({text:(e.innerText||e.value||'').trim(),disabled:!!e.disabled,busy:e.getAttribute('aria-busy')||'',react:reactInfo(e)}))}));
      const captcha={hcaptcha_type:typeof window.hcaptcha,grecaptcha_type:typeof window.grecaptcha,visible_widgets:challengeSignals.filter(x=>x.interactive).length,hidden_response_fields:[...document.querySelectorAll('textarea[name*=captcha i],input[name*=captcha i]')].map(e=>({name:e.name||'',present:!!e.value}))};
      return{url:location.href,application_form:applicationForm?{id:applicationForm.id||'',name:applicationForm.getAttribute('name')||'',action:applicationForm.action||''}:null,form_valid:activeForms.map(f=>[...f.elements].every(e=>!e.willValidate||e.validity.valid)),errors:[...new Set(errors)].slice(0,40),invalid_fields:invalid,challenges:challengeSignals.filter(x=>x.interactive),passive_challenge_signals:challengeSignals.filter(x=>!x.interactive),submitters,forms,captcha,dom_events:window.__externalAgentSubmission||{},body:document.body.innerText.slice(0,5000)}}""")
    dom["runtime"]=await runtime_metadata()
    dom["captcha"]["flow"]=await captcha_flow_metadata()
    required=await required_state()
    dom["required_fields"]=required["required_fields"]
    dom["missing_required_fields"]=required["missing_required_fields"]
    dom["network"]=[x for x in network_events if x["time"]>=since]
    dom=await stabilize_challenges(dom,"snapshot")
    return dom

async def required_state():
    """Return an actionable, privacy-safe checklist for required controls."""
    data=await page.evaluate(r"""() => {
      const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
        return !!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden')};
      const label=e=>{
        const aria=e.getAttribute('aria-label'); if(aria) return aria.trim();
        const by=e.getAttribute('aria-labelledby');
        if(by){const text=by.split(/\s+/).map(id=>document.getElementById(id)?.innerText||'').join(' ').trim();if(text)return text}
        if(e.labels?.length)return [...e.labels].map(x=>x.innerText.trim()).filter(Boolean).join(' / ');
        const parent=e.closest('fieldset,section,div');
        const parentText=(parent?.innerText||'').trim().replace(/\s+/g,' ');
        return (e.getAttribute('placeholder')||e.name||e.id||e.getAttribute('role')||parentText.slice(0,160)||'').trim();
      };
      const selector=e=>{
        if(e.id)return '#'+CSS.escape(e.id);
        if(e.name)return `${e.tagName.toLowerCase()}[name="${CSS.escape(e.name)}"]`;
        const siblings=e.parentElement?[...e.parentElement.children].filter(x=>x.tagName===e.tagName):[];
        const index=Math.max(1,siblings.indexOf(e)+1);
        return `${e.tagName.toLowerCase()}:nth-of-type(${index})`;
      };
      const required=e=>!!e.required||e.getAttribute('aria-required')==='true'||e.dataset.required==='true'||e.getAttribute('data-required')==='true';
      const valuePresent=e=>{
        if(e.type==='file') return !!e.files?.length;
        if(e.matches?.('[role="group"]')&&e.querySelector?.('input[type="file"]')) return [...e.querySelectorAll('input[type="file"]')].some(x=>x.files?.length);
        if(e.type==='radio'){
          const name=e.name;
          if(!name) return !!e.checked;
          return [...scope.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`)].some(x=>x.checked);
        }
        if(e.type==='checkbox') return !!e.checked;
        if(e.tagName==='SELECT'&&e.multiple) return [...e.selectedOptions].length>0;
        if(e.getAttribute('role')==='combobox'||e.getAttribute('role')==='listbox'){
          // React-Select clears the editable input after a choice is
          // committed.  The selected value is rendered in a sibling
          // `singleValue` node and the actual form value is kept in a hidden
          // required input.  Checking only e.value therefore reports a real
          // selection as missing and makes the agent retry the same widget.
          const root=e.closest('.select-shell,[class*="__container"],[class*="control"],fieldset,section')||e.parentElement;
          const visible=x=>{const r=x.getBoundingClientRect(),s=getComputedStyle(x);return !!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden')};
          const selected=[...(root?.querySelectorAll?.('[class*="singleValue" i],[class*="single-value" i],[data-selected="true"],[aria-selected="true"]')||[])].find(x=>visible(x)&&(x.innerText||x.getAttribute('aria-label')||'').trim());
          const hidden=[...(root?.querySelectorAll?.('input[type="hidden"],input[aria-hidden="true"]')||[])].find(x=>String(x.value||'').trim());
          return !!((e.value||e.innerText||e.getAttribute('aria-activedescendant')||selected?.innerText||selected?.getAttribute('aria-label')||hidden?.value||'').trim());
        }
        return !!String(e.value??'').trim();
      };
      const allForms=[...document.forms];
      const applicationForm=allForms.find(f=>f.id==='job-application-form')||allForms
        .filter(f=>f.querySelector('input[type=file],input[name*="candidate" i],textarea[name*="candidate" i],[id*="candidate" i]'))
        .sort((a,b)=>Number(/application|apply|candidate/i.test(b.id+' '+b.name+' '+b.action))-Number(/application|apply|candidate/i.test(a.id+' '+a.name+' '+a.action)))[0]||null;
      const scope=applicationForm||document;
      const controls=[...scope.querySelectorAll('input,select,textarea,[role=combobox],[role=listbox],[aria-required=true]')]
        .filter(e=>{
          if(e.disabled)return false;
          if(required(e))return true;
          const text=label(e).toLowerCase();
          // Some ATS forms omit the required attribute on the location and
          // resume controls.  Treat those visible controls as required for
          // handoff readiness so a CAPTCHA cannot be handed to a person on a
          // blank initial form.
          if(e.type==='file'&&/(resume|cv|curriculum)/i.test(text))return true;
          if(e.tagName==='SELECT'&&/(location|country|region|office)/i.test(text))return true;
          return false;
        });
      const fields=controls.map(e=>{
        const nativeValid=e.willValidate?e.validity.valid:true;
        const present=valuePresent(e);
        return {selector:selector(e),id:e.id||'',name:e.name||'',tag:e.tagName.toLowerCase(),type:e.type||'',role:e.getAttribute('role')||'',label:label(e),visible:visible(e),valid:nativeValid&&present,present,required:true,message:e.validationMessage||''};
      });
      const missing=fields.filter(f=>f.visible&&(!f.valid||!f.present)).map(f=>({selector:f.selector,id:f.id,name:f.name,tag:f.tag,type:f.type,role:f.role,label:f.label,message:f.message||'required value missing'}));
      return {required_fields:fields,missing_required_fields:missing,application_form:applicationForm?{id:applicationForm.id||'',name:applicationForm.getAttribute('name')||'',action:applicationForm.action||''}:null,form_valid:(applicationForm?[applicationForm]:allForms).every(f=>f.checkValidity())};
    }""")
    if resume_upload_confirmed:
        # Greenhouse removes/replaces the native file input after its S3
        # upload succeeds.  The employer state is then represented by the
        # uploaded-file widget rather than an input.files list.
        for field in data.get("required_fields",[]):
            label=str(field.get("label") or "").casefold()
            if any(token in label for token in ("resume","cv","curriculum")):
                field["present"]=True; field["valid"]=True
        data["missing_required_fields"]=[field for field in data.get("missing_required_fields",[]) if not any(token in str(field.get("label") or "").casefold() for token in ("resume","cv","curriculum"))]
    return data

async def widget_metadata():
    """Describe visible custom-widget options without recording applicant data."""
    return await page.evaluate(r"""() => {
      const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return !!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden')};
      const nodes=[...document.querySelectorAll('[role=option],[role=listbox],[role=menuitem],.pac-item,.iti__country,[data-value],[data-option],[class*="option" i],[class*="suggestion" i]')];
      return nodes.filter(visible).slice(0,100).map(e=>({tag:e.tagName.toLowerCase(),id:e.id||'',role:e.getAttribute('role')||'',class:String(e.className||'').slice(0,160),text:(e.innerText||e.getAttribute('aria-label')||e.getAttribute('data-value')||'').trim().replace(/\s+/g,' ').slice(0,240),selector:e.id?('#'+CSS.escape(e.id)):''}));
    }""")

async def combobox_state(el):
    """Read the committed value of native and React-Select comboboxes.

    React-Select deliberately leaves its search input empty after selection;
    the selected label and hidden required input are the authoritative state.
    """
    return await el.evaluate(r"""e=>{
      const visible=x=>{const r=x.getBoundingClientRect(),s=getComputedStyle(x);return !!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden')};
      const root=e.closest('.select-shell,[class*="__container"],[class*="control"],fieldset,section')||e.parentElement;
      const selected=[...(root?.querySelectorAll?.('[class*="singleValue" i],[class*="single-value" i],[data-selected="true"],[aria-selected="true"]')||[])].find(x=>visible(x)&&(x.innerText||x.getAttribute('aria-label')||'').trim());
      const hidden=[...(root?.querySelectorAll?.('input[type="hidden"],input[aria-hidden="true"]')||[])].find(x=>String(x.value||'').trim());
      return {value:e.value||'',text:(e.innerText||'').trim(),active:e.getAttribute('aria-activedescendant')||'',selected:(selected?.innerText||selected?.getAttribute('aria-label')||'').trim(),hidden:(hidden?.value||'').trim()};
    }""")

RECEIPT_PHRASES=("thank you for applying","we have received your application","thanks for applying","application submitted","application received","your application has been submitted","your application has been received","application successfully submitted","application has been successfully submitted","your application has been successfully submitted","your application was successfully submitted","sollicitatie ontvangen","bedankt voor je sollicitatie","bedankt voor uw sollicitatie","bedankt voor je bericht. het is verzonden","bedankt voor uw bericht. het is verzonden","sollicitatie is succesvol ingediend","je sollicitatie is succesvol ingediend","uw sollicitatie is succesvol ingediend","bewerbung eingegangen")
def receipts(text):
    text=text.casefold()
    return {phrase for phrase in RECEIPT_PHRASES if phrase in text}

async def wait_for_human_handoff():
    """Keep the MCP-owned page alive while the user completes a challenge.

    This is deliberately observation-only. The human performs the challenge
    and Send click; the server only watches for a new employer receipt.
    """
    focused=False
    if focus_external_browser is not None and page is not None:
        try:
            # Foregrounding the Edge window does not select a tab. Select the
            # MCP-owned application tab first so the human sees the filled
            # form/challenge rather than a LinkedIn detail tab in the same
            # window.
            await page.bring_to_front()
            focused=bool(focus_external_browser(page=page))
            await page.bring_to_front()
        except Exception as error:
            event("handoff_focus_error",error=type(error).__name__)
    notify_handoff_required(source="external_agent_ready")
    event("handoff_started",timeout_seconds=handoff_timeout,focused=focused,url=urlsplit(page.url)._replace(query="",fragment="").geturl() if page is not None else "")
    end=time.monotonic()+max(1,handoff_timeout)
    last_state=None
    while time.monotonic()<end:
        try:
            if page is None or page.is_closed():
                event("handoff_page_unavailable")
            else:
                evidence=await snapshot()
                found=receipts(evidence.get("body",""))-baseline_receipts
                if found:
                    receipt=sorted(found)[0]
                    event("handoff_receipt",receipt=receipt,url=urlsplit(evidence.get("url",""))._replace(query="",fragment="").geturl())
                    return receipt
                flow=(evidence.get("captcha") or {}).get("flow") or {}
                send_busy=any(str(item.get("text","")).casefold()=="send" and str(item.get("busy","")).casefold()=="true" for item in (evidence.get("submitters") or []))
                state=(evidence.get("url",""),len(evidence.get("body","") or ""),len(evidence.get("challenges") or []),int(flow.get("visible_widgets",0) or 0),bool(flow.get("response_present")),send_busy)
                if state!=last_state:
                    event("handoff_snapshot",url=urlsplit(evidence.get("url",""))._replace(query="",fragment="").geturl(),body_chars=state[1],visible_challenges=state[2],visible_widgets=state[3],response_present=state[4],send_busy=state[5])
                    last_state=state
        except Exception as error:
            # A user challenge can briefly recreate the page. Keep the
            # handoff alive instead of treating that transient error as exit.
            event("handoff_observation_error",error=type(error).__name__)
        await asyncio.sleep(2)
    event("handoff_timeout",timeout_seconds=handoff_timeout)
    return ""
def log_snapshot(stage,data):
    # Do not persist form body, field values, or uploaded contents.
    safe={k:data.get(k) for k in ("form_valid","errors","invalid_fields","missing_required_fields","required_fields","challenges","submitters","forms","captcha","dom_events","network","runtime")}
    safe["url"]=urlsplit(data.get("url",""))._replace(query="",fragment="").geturl()
    safe["failure_code"]=classify_submission_state(data)
    event("submission_diagnostics",stage=stage,attempt=submission_attempts,**safe)

async def stabilize_challenges(data, stage):
    """Require a CAPTCHA signal to remain interactive across a short recheck.

    ATS pages often insert an hCaptcha iframe briefly while the form hydrates.
    A single DOM snapshot can therefore report a visible challenge even though
    it is hidden by the time a user could interact with the form.  Keep a real
    challenge blocking, but classify a signal that disappears on recheck as
    passive metadata so it does not stop a valid application.
    """
    if data is None:
        return data
    if "metadata" in data:
        initial=data.get("metadata",{}).get("challenges") or []
    else:
        initial=data.get("challenges") or []
    if not initial:
        return data
    try:
        await page.wait_for_timeout(900)
        current=await page.evaluate("""() => {
          const nodes=[...document.querySelectorAll('iframe[src*=captcha i],iframe[src*=recaptcha i],iframe[src*=hcaptcha i],iframe[src*=turnstile i],[class*=captcha i],[id*=captcha i],[class*=turnstile i],[id*=turnstile i],[class*=hcaptcha i]')];
          return nodes.map(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e),marker=(e.id+' '+String(e.className||'')).toLowerCase(),errorNode=e.getAttribute('role')==='alert'||/(^|[-_ ])(error|invalid)([-_ ]|$)/.test(marker),hasMeaningfulText=!!(e.innerText||'').trim(),interactive=!!(r.width>=30&&r.height>=30&&s.display!=='none'&&s.visibility!=='hidden'&&!errorNode&&(e.tagName==='IFRAME'||hasMeaningfulText||e.tabIndex>=0));return {tag:e.tagName.toLowerCase(),src:e.src||'',title:e.title||'',text:(e.innerText||'').trim().slice(0,200),interactive}}).filter(x=>x.interactive);
        }""")
        if "metadata" in data:
            passive=data.setdefault("metadata",{}).setdefault("passive_challenge_signals",[])
            if current:
                data["metadata"]["challenges"]=current
            else:
                data["metadata"]["challenges"]=[]
                for item in initial:
                    if item not in passive: passive.append(item)
        else:
            passive=data.setdefault("passive_challenge_signals",[])
            if current:
                data["challenges"]=current
            else:
                data["challenges"]=[]
                for item in initial:
                    if item not in passive: passive.append(item)
        event("challenge_recheck",stage=stage,initial=len(initial),stable=len(current),transient=not bool(current))
    except Exception as error:
        event("challenge_recheck_error",stage=stage,error=type(error).__name__)
    return data

@mcp.tool()
async def open_application(url:str)->str:
    """Open only the approved URL.

    Edge handoff pages must survive the Codex/MCP worker ending.  Therefore an
    Edge run uses the browser's persistent CDP context and creates only a new
    page; isolated temporary contexts remain reserved for Lightpanda/test
    backends.  This lets a deferred CAPTCHA page stay visible while the next
    application opens in a separate tab.
    """
    global pw,browser,context,page,current_endpoint,active_backend
    guard(); validate_url(url)
    if context is not None and active_backend != "edge":
        await context.close()
    endpoint=fallback_endpoint if fallback_endpoint and any(e.get("kind")=="fallback_recommended" for e in events) else initial_endpoint
    backend="edge" if endpoint==fallback_endpoint and fallback_endpoint else initial_backend
    if pw is None:
        pw=await async_playwright().start()
    if browser is None or current_endpoint!=endpoint:
        previous=active_backend
        browser=await pw.chromium.connect_over_cdp(endpoint,timeout=10000)
        current_endpoint=endpoint; active_backend=backend
        if previous!=active_backend: event("browser_switch",from_backend=previous,to_backend=active_backend,endpoint=endpoint)
    if active_backend == "edge":
        # CDP exposes the user's persistent Edge context.  Never close it from
        # this worker: retained CAPTCHA pages must remain available after the
        # worker exits and while another worker opens a new page.
        contexts=browser.contexts
        context=contexts[0] if contexts else await browser.new_context()
    else:
        context=await browser.new_context()
    page=await context.new_page()
    attach_network(page); await page.goto(url,wait_until="domcontentloaded",timeout=45000); await install_observers(); event("open",url=page.url,backend=active_backend); return "Opened "+page.url

@mcp.tool()
async def inspect()->dict:
    """Read controls plus form metadata, submit controls, hidden required fields, and CAPTCHA signals."""
    guard()
    data=await page.evaluate("""() => {const v=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return!!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden')};
      const controls=[...document.querySelectorAll('input,select,textarea,button,[role=button],[role=combobox]')].map(e=>({tag:e.tagName.toLowerCase(),type:e.type||'',id:e.id||'',name:e.name||'',label:[...(e.labels||[])].map(x=>x.innerText.trim()).join(' / '),text:(e.innerText||e.value||'').trim(),aria:e.getAttribute('aria-label')||'',placeholder:e.placeholder||'',visible:v(e),required:!!e.required||e.getAttribute('aria-required')==='true',disabled:!!e.disabled,pattern:e.pattern||'',autocomplete:e.autocomplete||'',value:e.type==='file'?(e.files?.[0]?.name||''):e.value||'',checked:!!e.checked,valid:e.willValidate?e.validity.valid:null,message:e.validationMessage||'',options:e.tagName==='SELECT'?[...e.options].map(o=>({label:o.text.trim(),value:o.value,selected:o.selected})):[]}));
      const forms=[...document.forms].map((f,index)=>({index,action:f.action||'',method:(f.method||'get').toUpperCase(),no_validate:f.noValidate,valid:f.checkValidity(),visible_required:[...f.querySelectorAll('[required],[aria-required=true]')].filter(v).map(e=>({id:e.id,name:e.name,type:e.type||'',valid:e.willValidate?e.validity.valid:true})),hidden_required:[...f.querySelectorAll('[required],[aria-required=true]')].filter(e=>!v(e)).map(e=>({id:e.id,name:e.name,type:e.type||''}))}));
      const submit_controls=controls.filter(e=>e.visible&&!e.disabled&&(e.type==='submit'||/^(send|submit|apply|continue|next)$/i.test(e.text||'')||/^(send|submit|apply|continue|next)$/i.test(e.aria||'')));
      const challengeNodes=[...document.querySelectorAll('iframe[src*=captcha i],iframe[src*=recaptcha i],iframe[src*=hcaptcha i],iframe[src*=turnstile i],[class*=captcha i],[id*=captcha i],[class*=turnstile i],[id*=turnstile i],[class*=hcaptcha i]')];
      const challengeSignals=challengeNodes.map(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e),marker=(e.id+' '+String(e.className||'')).toLowerCase(),errorNode=e.getAttribute('role')==='alert'||/(^|[-_ ])(error|invalid)([-_ ]|$)/.test(marker),hasMeaningfulText=!!(e.innerText||'').trim(),interactive=!!(r.width>=30&&r.height>=30&&s.display!=='none'&&s.visibility!=='hidden'&&!errorNode&&(e.tagName==='IFRAME'||hasMeaningfulText||e.tabIndex>=0));return {tag:e.tagName.toLowerCase(),src:e.src||'',title:e.title||'',text:(e.innerText||'').trim().slice(0,200),interactive}});
      return{url:location.href,text:document.body.innerText.slice(0,12000),controls,metadata:{forms,submit_controls,challenges:challengeSignals.filter(x=>x.interactive),passive_challenge_signals:challengeSignals.filter(x=>!x.interactive)}}}""")
    data["metadata"]["runtime"]=await runtime_metadata()
    data=await stabilize_challenges(data,"inspect")
    required=await required_state()
    data["metadata"]["required_fields"]=required["required_fields"]
    data["metadata"]["missing_required_fields"]=required["missing_required_fields"]
    data["metadata"]["visible_widget_options"]=await widget_metadata()
    event("inspect",controls=len(data["controls"]),challenges=len(data["metadata"]["challenges"]),missing_required=len(required["missing_required_fields"]))
    return data

@mcp.tool()
async def click(selector:str)->str:
    """Click one visible unique non-final control. Do not use for final submission."""
    try:
        guard(); el=await one(selector)
        if not await el.is_visible() or not await el.is_enabled(): raise ValueError("Control hidden or disabled")
        method="dom_fallback" if active_backend=="lightpanda" else "playwright"
        if method=="playwright": await el.click(timeout=10000)
        else: await el.evaluate("e=>e.click()")
        await page.wait_for_timeout(400); validate_url(page.url); event("click",selector=selector,method=method); return "clicked"
    except Exception as error:
        action_failed("click",selector,error); raise
@mcp.tool()
async def click_text(text:str)->str:
    """Click largest visible enabled non-final button/link with exact text; use only entry/navigation."""
    try:
        guard(); candidates=[]
        for el in await page.locator("button,a,[role=button]").all():
            if await el.is_visible() and await el.is_enabled() and (await el.inner_text()).strip().casefold()==text.strip().casefold():
                candidates.append((await el.evaluate("e=>{const r=e.getBoundingClientRect();return r.width*r.height}"),el))
        if not candidates: raise ValueError("No visible enabled control with exact text: "+text)
        _,el=max(candidates,key=lambda x:x[0]); method="dom_fallback" if active_backend=="lightpanda" else "playwright"
        if method=="playwright": await el.click(timeout=10000)
        else: await el.evaluate("e=>e.click()")
        await page.wait_for_timeout(400); validate_url(page.url); event("click_text",text=text,method=method); return "clicked"
    except Exception as error:
        action_failed("click_text",text,error); raise
@mcp.tool()
async def fill(selector:str,value:str)->str:
    """Fill a visible unique input/textarea and return exact retained value plus validation."""
    guard(); el=await one(selector)
    if not await el.is_visible() or not await el.is_enabled(): raise ValueError("Field hidden or disabled")
    await el.fill(value,timeout=10000); await page.wait_for_timeout(250); kept=await el.input_value(); valid=await el.evaluate("e=>e.willValidate?e.validity.valid:true")
    event("fill",selector=selector,retained=kept==value,valid=valid); return json.dumps({"retained_value":kept,"exact_match":kept==value,"valid":valid})
@mcp.tool()
async def select(selector:str,value:str)->str:
    """Select native SELECT by exact inspected option value."""
    guard(); el=await one(selector)
    if not await el.is_visible() or not await el.is_enabled(): raise ValueError("Select hidden or disabled")
    await el.select_option(value=value,timeout=10000); event("select",selector=selector,value=value); return await el.input_value()
@mcp.tool()
async def set_checked(selector:str,checked:bool=True)->str:
    """Set a unique checkbox/radio via its visible label when needed; verify retained state."""
    guard(); el=await one(selector)
    if not await el.is_enabled(): raise ValueError("Choice disabled")
    applied=await el.evaluate("""(e,desired)=>{
      if(!['checkbox','radio'].includes(e.type))throw Error('Not a native checkbox or radio');
      if(e.type==='radio'&&!desired)throw Error('Select another radio to change a radio group');
      if(e.checked===desired)return true;
      const visible=x=>{const r=x.getBoundingClientRect(),s=getComputedStyle(x);return !!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden')};
      if(visible(e)) e.click(); else {const labels=[...(e.labels||[])].filter(visible);if(labels.length!==1)throw Error('No unique visible label');labels[0].click()}
      return e.checked===desired;
    }""",checked)
    if not applied: raise RuntimeError("Choice selection was not retained")
    event("checked",selector=selector,checked=checked); return str(await el.is_checked())

@mcp.tool()
async def press_key(selector:str,key:str)->str:
    """Press one keyboard key on a visible control and verify the page remains."""
    try:
        guard(); el=await one(selector)
        if not await el.is_visible() or not await el.is_enabled(): raise ValueError("Control hidden or disabled")
        await el.press(key,timeout=10000); await page.wait_for_timeout(350); validate_url(page.url)
        event("press_key",selector=selector,key=key); return "pressed"
    except Exception as error:
        action_failed("press_key",selector,error); raise

@mcp.tool()
async def select_custom_option(selector:str,option_text:str)->str:
    """Select an option from a React/phone custom combobox by visible text.

    The helper clicks the inspected trigger, prefers an exact visible option,
    and falls back to typing into an editable combobox followed by Enter.  It
    never selects hidden options or uses a JS-only click.
    """
    try:
        guard(); trigger=await one(selector)
        if not await trigger.is_visible() or not await trigger.is_enabled(): raise ValueError("Control hidden or disabled")
        if active_backend=="lightpanda": await trigger.evaluate("e=>e.click()")
        else: await trigger.click(timeout=10000)
        await page.wait_for_timeout(700)
        wanted=" ".join(str(option_text).split()).casefold()
        search_terms=[wanted]
        # Phone widgets commonly render the country as "Netherlands" while
        # the agent knows it as "Netherlands +31".  Search the display name
        # and the full label without relaxing the selection to hidden nodes.
        base=wanted.split("+")[0].strip()
        if base and base not in search_terms: search_terms.append(base)
        selectors=("[role=option]","button[role=option]",".pac-item",".iti__country","[role=menuitem]","option","li","[data-value]","[data-option]","[class*='option' i]","[class*='suggestion' i]")
        candidates=[]
        for css in selectors:
            for el in await page.locator(css).all():
                if not await el.is_visible() or not await el.is_enabled(): continue
                text=" ".join(((await el.inner_text()) or "").split())
                aria=" ".join(((await el.get_attribute("aria-label")) or "").split())
                value=" ".join(((await el.get_attribute("data-value")) or "").split())
                code=" ".join(((await el.get_attribute("data-country-code")) or "").split())
                haystack=" | ".join(x.casefold() for x in (text,aria,value,code) if x)
                if any(term and (haystack==term or term in haystack) for term in search_terms):
                    candidates.append(el)
            if candidates: break
        if candidates:
            # Duplicate desktop/mobile option nodes are common.  The query
            # order prefers the active role=option list; for identical visible
            # labels the first enabled match is the same user choice.
            target=candidates[0]
            if active_backend=="lightpanda": await target.evaluate("e=>e.click()")
            else: await target.click(timeout=10000)
            await page.wait_for_timeout(500); validate_url(page.url)
            state=await combobox_state(trigger)
            retained=bool(state.get("value") or state.get("active") or state.get("text") or state.get("selected") or state.get("hidden"))
            event("select_custom_option",selector=selector,option=option_text,method="visible_option",retained=retained)
            if retained or await trigger.get_attribute("role") not in {"combobox","listbox"}:
                return "selected"
            # Some React widgets attach the click handler to the option's
            # child and leave the input value unchanged.  Give the same
            # inspected combobox a normal keyboard commit before failing.
            await trigger.press("ArrowDown",timeout=10000)
            await trigger.press("Enter",timeout=10000)
            await page.wait_for_timeout(400)
            state=await combobox_state(trigger)
            if state.get("value") or state.get("active") or state.get("text") or state.get("selected") or state.get("hidden"):
                event("select_custom_option",selector=selector,option=option_text,method="visible_option_keyboard_recovery",retained=True)
                return "selected"
            raise ValueError(f"Option click did not retain a selection: {option_text}")
        # Searchable phone/country widgets often expose no list item until a
        # query is typed.  Use the native key path, then verify retained text.
        input_type=await trigger.get_attribute("type")
        if input_type not in {"hidden","checkbox","radio","file"}:
            query=base or option_text
            await trigger.fill(query,timeout=10000)
            await page.wait_for_timeout(1200)
            # Searchable widgets render their option list only after the
            # query is typed.  Re-scan the visible list before using keyboard
            # fallback so location and production Yes/No fields commit the
            # actual option rather than merely retaining search text.
            typed_candidates=[]
            for css in selectors:
                for el in await page.locator(css).all():
                    if not await el.is_visible() or not await el.is_enabled(): continue
                    text=" ".join(((await el.inner_text()) or "").split())
                    aria=" ".join(((await el.get_attribute("aria-label")) or "").split())
                    value=" ".join(((await el.get_attribute("data-value")) or "").split())
                    code=" ".join(((await el.get_attribute("data-country-code")) or "").split())
                    haystack=" | ".join(x.casefold() for x in (text,aria,value,code) if x)
                    if any(term and (haystack==term or term in haystack) for term in search_terms): typed_candidates.append(el)
                if typed_candidates: break
            if typed_candidates:
                target=typed_candidates[0]
                if active_backend=="lightpanda": await target.evaluate("e=>e.click()")
                else: await target.click(timeout=10000)
                await page.wait_for_timeout(500); validate_url(page.url)
                state=await combobox_state(trigger)
                if state.get("value") or state.get("active") or state.get("text") or state.get("selected") or state.get("hidden"):
                    event("select_custom_option",selector=selector,option=option_text,method="typed_visible_option",retained=True)
                    return "selected"
            await trigger.press("ArrowDown",timeout=10000)
            await trigger.press("Enter",timeout=10000)
            await page.wait_for_timeout(400); validate_url(page.url)
            state=await combobox_state(trigger)
            retained=(state.get("value") or state.get("selected") or state.get("hidden") or state.get("active") or state.get("text") or "").strip()
            if retained:
                event("select_custom_option",selector=selector,option=option_text,method="keyboard",retained=retained[:120])
                return "selected"
        raise ValueError(f"No visible enabled option matched: {option_text}")
    except Exception as error:
        action_failed("select_custom_option",f"{selector}:{option_text}",error); raise

async def _upload_document(selector:str, path:Path, role:str)->str:
    """Upload one semantically identified document to a unique file input."""
    global active_upload_role
    guard(); last=None; active_upload_role=role
    upload_event_name = "resume_upload" if role == "resume" else "cover_letter_upload"
    for attempt in range(1,3):
        try:
            # React ATS forms can replace the file input after the S3 upload;
            # re-resolve the selector before every attempt instead of keeping a
            # detached ElementHandle.
            target=page.locator(selector)
            count=await target.count()
            if count==0: raise ValueError("No file input matched: "+selector)
            if count==1:
                el=target
            else:
                attached=[]
                for index in range(count):
                    candidate=target.nth(index)
                    try:
                        if await candidate.is_enabled() and await candidate.evaluate("e=>e.isConnected"):
                            attached.append(candidate)
                    except Exception: continue
                if not attached: raise ValueError("No attached enabled file input matched: "+selector)
                el=attached[-1]
            await el.set_input_files(str(path),timeout=10000)
            # Playwright's set_input_files already emits input/change.  Some
            # Greenhouse builds immediately replace the native input after
            # accepting the S3 upload; dispatching a second event on that
            # detached handle otherwise waits for the default 30s timeout.
            await page.wait_for_timeout(700)
            if role in upload_confirmed_roles or (role == "resume" and resume_upload_confirmed):
                accepted={"files":[{"name":path.name}],"valid":True,"accepted_via_network":True}
                event(f"{upload_event_name}_accepted",selector=selector,file=path.name,attempt=attempt,reason="network_after_set_input_files",role=role)
                return json.dumps(accepted)
            try:
                attached=bool(await el.evaluate("e=>e.isConnected"))
            except Exception:
                attached=False
            if attached:
                for dispatch_name in ("input","change"):
                    try: await el.dispatch_event(dispatch_name,timeout=1500)
                    except Exception as dispatch_error:
                        event(f"{role}_upload_dispatch_skipped",selector=selector,event=dispatch_name,error=type(dispatch_error).__name__)
            await page.wait_for_timeout(300)
            if role in upload_confirmed_roles or (role == "resume" and resume_upload_confirmed):
                accepted={"files":[{"name":path.name}],"valid":True,"accepted_via_network":True}
                event(f"{upload_event_name}_accepted",selector=selector,file=path.name,attempt=attempt,reason="network_after_dispatch",role=role)
                return json.dumps(accepted)
            selected=await el.evaluate("e=>({files:[...e.files].map(f=>({name:f.name,size:f.size})),valid:e.willValidate?e.validity.valid:true,message:e.validationMessage||'',ariaInvalid:e.getAttribute('aria-invalid')})")
            event(upload_event_name,selector=selector,file=path.name,attempt=attempt,selected=selected,role=role)
            if selected.get("files"):
                return json.dumps(selected)
            raise RuntimeError("file input retained no file after upload")
        except Exception as error:
            last=error
            event(f"{role}_upload_retry",selector=selector,file=path.name,attempt=attempt,error=type(error).__name__,detail=str(error)[:500])
            if role in upload_confirmed_roles or (role == "resume" and resume_upload_confirmed):
                accepted={"files":[{"name":path.name}],"valid":True,"accepted_via_network":True}
                event(f"{upload_event_name}_accepted",selector=selector,file=path.name,attempt=attempt,role=role)
                return json.dumps(accepted)
            if attempt<2:
                await page.wait_for_timeout(500)
    action_failed(f"upload_{role}",selector,last or RuntimeError(f"{role} upload failed"))
    raise last or RuntimeError(f"{role} upload failed")

@mcp.tool()
async def upload_resume(selector:str)->str:
    """Upload only the configured primary resume to a unique file input."""
    path=Path(os.environ["EXTERNAL_AGENT_RESUME"]).resolve(strict=True)
    return await _upload_document(selector,path,"resume")

@mcp.tool()
async def upload_cover_letter(selector:str)->str:
    """Upload only the job-specific Cover Letter returned by the cover-letter session."""
    configured=str(os.environ.get("EXTERNAL_AGENT_COVER_LETTER","")).strip()
    if not configured:
        raise ValueError("No job-specific external cover letter was configured")
    path=Path(configured).resolve(strict=True)
    return await _upload_document(selector,path,"cover_letter")
@mcp.tool()
async def diagnose_validation()->dict:
    """Map validation, file state, challenge widgets and network evidence after a failed action."""
    guard(); data=await snapshot()
    data["file_fields"]=await page.evaluate("""() => [...document.querySelectorAll('input[type=file]')].map(e=>({name:e.name,id:e.id,required:e.required,valid:e.willValidate?e.validity.valid:true,message:e.validationMessage||'',files:[...e.files].map(f=>({name:f.name,size:f.size}))}))""")
    event("diagnose_validation",errors=len(data["errors"]),invalid=len(data["invalid_fields"]),missing_required=len(data.get("missing_required_fields") or []),missing_required_fields=data.get("missing_required_fields") or [],challenges=len(data["challenges"]),failure_code=classify_submission_state(data))
    if submission_attempts: log_snapshot("diagnosis",data)
    return data
@mcp.tool()
async def submit_final()->dict:
    """Make one instrumented final submission. At most 3 attempts total. On a stuck form reopen, refill and retry; stop on receipt."""
    global submission_attempts,baseline_receipts,receipt_observed_after_submit
    guard(); await install_observers(); before=await snapshot()
    found=receipts(before["body"])
    if submission_attempts and found-baseline_receipts:
        return {"attempted":False,"reason":"receipt_already_visible","receipt":sorted(found-baseline_receipts)}
    if submission_attempts>=3:
        return {"attempted":False,"reason":"attempt_limit","attempts":submission_attempts}
    missing=before.get("missing_required_fields") or []
    if missing: return {"attempted":False,"reason":"required_fields_missing","missing_required_fields":missing,"evidence":before}
    if before["challenges"]: return {"attempted":False,"reason":"challenge_detected","evidence":before}
    if before["invalid_fields"]: return {"attempted":False,"reason":"invalid_fields","evidence":before}
    # Some ATS pages render the real application form beside an unrelated
    # account/login form.  A global submitter count therefore becomes
    # ambiguous even though the application form has one deterministic
    # control.  Prefer a visible submitter owned by the valid form that
    # contains candidate/file fields; fall back to the historical single
    # visible-submit rule when no form can be classified safely.
    all_candidates=await page.locator("form button[type=submit],form input[type=submit],button[type=submit],input[type=submit]").all()
    visible=[]
    for el in all_candidates:
        try:
            if not await el.is_visible() or not await el.is_enabled():
                continue
            owner=el.locator("xpath=ancestor::form[1]")
            form_meta={"id":"","name":"","action":"","valid":False,"candidate_fields":0}
            if await owner.count():
                form_meta=await owner.evaluate("""f=>({
                  id:f.id||'',name:f.getAttribute('name')||'',action:f.action||'',
                  valid:f.checkValidity(),
                  candidate_fields:f.querySelectorAll('input[type=file],input[name*="candidate" i],textarea[name*="candidate" i],[id*="candidate" i]').length
                })""")
            label=((await el.inner_text()) or (await el.get_attribute("value")) or "").strip()
            hay=" ".join(str(form_meta.get(k,"")) for k in ("id","name","action")).casefold()
            text=label.casefold()
            form_meta["application_form"]=bool(form_meta.get("candidate_fields",0)) and bool(form_meta.get("valid"))
            form_meta["application_named"]=any(term in hay for term in ("application","apply","candidate","job-application"))
            form_meta["submit_like"]=any(term in text for term in ("submit application","submit","send","apply"))
            visible.append({"element":el,"label":label,"form":form_meta})
        except Exception:
            continue
    preferred=[item for item in visible if item["form"].get("application_form")]
    if len(preferred)==1:
        selected=preferred[0]
    elif len(visible)==1:
        selected=visible[0]
    else:
        # A named application form with one submit-like control is also safe
        # when its browser-side validity is represented outside the native
        # form element (common in React/Rails ATS pages).
        named=[item for item in visible if item["form"].get("application_named") and item["form"].get("submit_like")]
        selected=named[0] if len(named)==1 else None
    if selected is None:
        # Ashby and some other React ATS pages render the final action as a
        # plain button outside any <form> (often without type=submit).  Only
        # accept an exact, visible application-submit label; never broaden
        # this fallback to arbitrary buttons such as navigation or login.
        exact=[]
        for el in await page.locator("button, [role=button]").all():
            try:
                if not await el.is_visible() or not await el.is_enabled():
                    continue
                label=((await el.inner_text()) or (await el.get_attribute("aria-label")) or (await el.get_attribute("value")) or "").strip()
                if label.casefold() in {"submit application","submit","send application"}:
                    exact.append({"element":el,"label":label,"form":{"id":"","name":"","action":"","valid":True,"candidate_fields":0,"application_form":False,"application_named":True,"submit_like":True}})
            except Exception:
                continue
        if len(exact)==1:
            selected=exact[0]
    if selected is None:
        labels=[item["label"] for item in visible]
        return {"attempted":False,"reason":"final_submit_ambiguous","visible_candidates":labels,"evidence":before}
    target=selected["element"]; label=selected["label"]; attempt_at=round(time.monotonic()-started,3); method="playwright"; click_interceptor=None
    if not submission_attempts: baseline_receipts=found
    submission_attempts+=1
    log_snapshot("before",before)
    try:
        # Edge uses Playwright's normal trusted, actionability-checked click.
        # A forced click can fire while an overlay or React state transition is
        # still settling, which makes a form look submitted while its handler
        # is not ready. Lightpanda keeps its DOM fallback for compatibility.
        if active_backend=="lightpanda":
            method="dom_fallback"; await target.evaluate("e=>e.click()")
        else:
            await target.click(timeout=10000)
    except Exception as err:
        # A normal trusted click can be blocked by a cookie/auth/pusher
        # overlay.  Capture the actual topmost element so the agent can
        # dismiss the right overlay instead of guessing.  If the submitter
        # itself is topmost, one trusted mouse click is a safe recovery;
        # never force-click through an unrelated overlay.
        try:
            click_interceptor=await target.evaluate("""e=>{
              const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
              const top=document.elementFromPoint(x,y);
              const visible=n=>{const b=n.getBoundingClientRect(),s=getComputedStyle(n);return !!(b.width&&b.height&&s.display!=='none'&&s.visibility!=='hidden')};
              const dialogs=[...document.querySelectorAll('dialog,[role=dialog],[aria-modal=true],[data-pusher-auth]')].filter(visible).slice(0,8).map(n=>({tag:n.tagName,id:n.id||'',class:String(n.className||'').slice(0,180),text:(n.innerText||'').trim().replace(/\\s+/g,' ').slice(0,300)}));
              return {x,y,top:top?{tag:top.tagName,id:top.id||'',class:String(top.className||'').slice(0,180),text:(top.innerText||top.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').slice(0,300)}:null,same_target:!!top&&(top===e||e.contains(top)),dialogs};
            }""")
        except Exception as inspect_error:
            click_interceptor={"inspection_error":type(inspect_error).__name__}
        event("submit_click_error",error=type(err).__name__,detail=str(err)[:1000],interceptor=click_interceptor)
        if click_interceptor and click_interceptor.get("same_target") and active_backend!="lightpanda":
            try:
                await page.mouse.click(float(click_interceptor["x"]),float(click_interceptor["y"]))
                method="mouse_fallback"
                event("submit_click_fallback",method=method,x=click_interceptor["x"],y=click_interceptor["y"])
            except Exception as fallback_error:
                event("submit_click_fallback_error",error=type(fallback_error).__name__,detail=str(fallback_error)[:800])
    await page.wait_for_timeout(2500); validate_url(page.url); evidence=await snapshot(attempt_at)
    log_snapshot("after",evidence)
    # Once a final submit has been attempted, a current visible employer
    # receipt is authoritative even if the same phrase appeared in a hidden
    # template or the pre-submit baseline.  Subtracting baseline receipts here
    # caused successful long pages (for example Locus) to be written as
    # unconfirmed when the receipt was below the 5,000-character snapshot.
    full_body=""
    try:
        full_body=(await page.locator("body").inner_text(timeout=5000)).casefold()
    except Exception:
        full_body=evidence.get("body","")
    after_receipts=receipts(full_body)
    new_receipts=after_receipts-baseline_receipts
    if new_receipts and application_request_seen(evidence.get("network") or []):
        receipt_observed_after_submit=True
    found=after_receipts if receipt_observed_after_submit else new_receipts
    fallback_recommended=hcaptcha_runtime_blocked(evidence["network"], evidence)
    if fallback_recommended:
        event("fallback_recommended",backend="edge",reason="hcaptcha_runtime_stalled_without_application_request",attempt=submission_attempts)
    failure_code=classify_submission_state(evidence)
    event("submit_final",attempt=submission_attempts,label=label,method=method,form_id=selected["form"].get("id",""),form_name=selected["form"].get("name",""),network=len(evidence["network"]),submits=len(evidence["dom_events"].get("submits",[])),receipt=sorted(found),failure_code=failure_code)
    return {"attempted":True,"attempt":submission_attempts,"remaining_attempts":3-submission_attempts,"receipt":sorted(found),"method":method,"label":label,"fallback_recommended":fallback_recommended,"failure_code":failure_code,"click_interceptor":click_interceptor,"evidence":evidence}
@mcp.tool()
async def reset_and_reupload_resume(selector:str)->str:
    """Clear/reupload only after diagnosis identifies this exact CV input as invalid or empty."""
    guard(); el=await one(selector); await el.set_input_files([]); await page.wait_for_timeout(300); return await upload_resume(selector)
@mcp.tool()
async def wait(seconds:float=1)->str:
    """Wait up to ten seconds for async state then inspect/diagnose again."""
    guard(); await page.wait_for_timeout(max(0,min(seconds,10))*1000); return "waited"
@mcp.tool()
async def candidate_context(question:str,open_ended:bool=False)->dict:
    """Return only truthful candidate data relevant to visible form questions."""
    root=Path(__file__).resolve().parent.parent
    import yaml
    config=yaml.safe_load((root/"config.yaml").read_text(encoding="utf-8")) or {}; profile=config.get("profile",{})
    raw_phone=str(profile.get("phone","")).strip()
    country=str(profile.get("country","")).strip().casefold()
    # Keep the configured national number for forms with a separate country
    # selector, and expose an international value for one-field phone widgets.
    digits=re.sub(r"[^0-9+]", "", raw_phone)
    international=raw_phone
    if digits.startswith("00"):
        international="+"+digits[2:]
    elif digits.startswith("+"):
        international=digits
    elif country in {"netherlands","the netherlands","nl"} and digits.startswith("0"):
        international="+31"+digits[1:]
    basics={key:profile.get(key,"") for key in ("first_name","last_name","email","phone","city","country","linkedin_url")}
    if raw_phone:
        basics["phone_international"]=international
    preferences=json.loads((root/"data"/"user_qa_preferences.json").read_text(encoding="utf-8")).get("preferences",{}); q=question.casefold(); matches=[]
    # The agent may summarize the form instead of repeating every label. In that
    # case use the visible page question text as a second, local-only matcher.
    page_text=""
    if page is not None:
        try: page_text=(await page.locator("body").inner_text(timeout=5000)).casefold()
        except Exception: page_text=""
    search_text=q+"\n"+page_text
    stop={"the","a","an","and","or","to","of","in","at","for","is","are","you","your","do","does","whether","can","will","be","this","that","with","on","it","we","our"}; q_tokens={x for x in re.findall(r"[a-z0-9]+",q) if x not in stop}
    for key,item in preferences.items():
        if not isinstance(item,dict): continue
        patterns=[str(x).casefold() for x in item.get("question_patterns",[])]; answer=str(item.get("answer","")).strip()
        def relevant(pattern):
            if pattern in search_text:return True
            tokens={x for x in re.findall(r"[a-z0-9]+",pattern) if x not in stop}
            search_tokens={x for x in re.findall(r"[a-z0-9]+",search_text) if x not in stop}
            return len(tokens)>=3 and len(tokens&search_tokens)>=3 and len(tokens&search_tokens)/len(tokens)>=.5
        if answer and answer not in {"待用户确认","待确认","unknown","Unknown","N/A"} and any(relevant(x) for x in patterns): matches.append({"id":key,"answer":answer})
    data={"basic_profile":basics,"matching_fixed_answers":matches}
    if open_ended:data["verified_resume_facts"]=(root/"data"/"applicant_context"/"resume_facts.md").read_text(encoding="utf-8")
    return data
@mcp.tool()
async def finish(reason:str)->dict:
    """End attempt and mark submitted only when an employer receipt is visible."""
    errors=[]; text=""; url=""; challenges=[]
    try: guard()
    except Exception as error: errors.append(f"guard:{type(error).__name__}")
    try:
        if page is not None:
            url=page.url
            text=(await page.locator("body").inner_text(timeout=5000)).lower()
            try:
                challenges=(await snapshot()).get("challenges") or []
            except Exception as error:
                errors.append(f"challenge_snapshot:{type(error).__name__}")
    except Exception as error:
        errors.append(f"page:{type(error).__name__}")
    found=(receipts(text) if receipt_observed_after_submit else receipts(text)-baseline_receipts) if submission_attempts else set()
    receipt=next(iter(sorted(found)),"")
    evidence={"body":text,"challenges":challenges}
    try:
        if page is not None:
            evidence=await snapshot()
    except Exception:
        pass
    if evidence:
        challenges=evidence.get("challenges") or challenges
        url=evidence.get("url") or url
        if not receipt:
            receipt=next(iter(sorted(receipts(evidence.get("body", "")))), "")
    failure_code="receipt_visible" if receipt else classify_submission_state(evidence)
    missing_required=evidence.get("missing_required_fields") or []
    # A challenge handoff is useful only after the form itself is complete.
    # Otherwise the human would solve CAPTCHA on a form that the agent still
    # cannot submit, which was the failure mode seen on Greenhouse.
    # A human handoff is meaningful only when the completed form visibly
    # contains an interactive challenge.  A missing token after Send without
    # a visible widget is a browser/runtime or employer-side stall; retain the
    # diagnostics and route it to the normal manual queue instead of opening a
    # blank/incomplete page for the user.
    handoff_required=(not missing_required) and (bool(challenges) or failure_code in {"visible_challenge","captcha_token_missing_visible_challenge"})
    if missing_required and not receipt:
        reason=f"{reason}; required fields remain incomplete: "+", ".join(str(x.get("label") or x.get("selector") or "unknown") for x in missing_required[:8])
    handoff_waited=False
    handoff_deferred=False
    if handoff_required and not receipt and defer_handoff:
        # Keep this Edge application context/page alive after the agent exits.
        # The caller can start another independent context/page immediately;
        # the retained page is later handled from the external handoff queue.
        handoff_deferred=True
        notify_handoff_required(source="external_agent_deferred")
        event("handoff_deferred",url=url,timeout_seconds=handoff_timeout)
        reason=f"{reason}; CAPTCHA handoff deferred without waiting so another application can run"
    if handoff_required and not receipt and not defer_handoff:
        handoff_waited=True
        if challenges or failure_code in {"visible_challenge","captcha_token_missing_visible_challenge"}:
            reason=f"{reason}; an interactive CAPTCHA was present in the live page; human handoff started"
        handoff_receipt=await wait_for_human_handoff()
        if handoff_receipt:
            receipt=handoff_receipt
            failure_code="receipt_visible"
            handoff_required=False
            reason=f"{reason}; employer receipt observed during human handoff: {handoff_receipt}"
        else:
            reason=f"{reason}; human handoff waited {handoff_timeout} seconds without an employer receipt"
    data={"status":"submitted" if receipt else "unconfirmed","reason":reason,"failure_code":failure_code,"receipt":receipt,"url":url,"backend":active_backend,"handoff_required":handoff_required,"handoff_waited":handoff_waited,"handoff_deferred":handoff_deferred,"handoff_timeout_seconds":handoff_timeout if handoff_waited else 0,"missing_required_fields":missing_required,"challenges":challenges,"finish_errors":errors,"events":events,"network":network_events}
    result_path.parent.mkdir(parents=True,exist_ok=True)
    if handoff_required:
        handoff_instruction=(
            "Complete the visible challenge in the retained Edge window, then click Send once. Do not refresh."
            if handoff_deferred else
            f"Complete the visible challenge in the focused Edge window, then click Send once. Do not refresh. The running process will wait up to {handoff_timeout} seconds and record a new employer receipt automatically."
        )
        handoff={"status":"waiting_for_human","url":url,"backend":active_backend,
                 "failure_code":failure_code,"created_at":time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "deferred":handoff_deferred,"instructions":handoff_instruction}
        (result_path.parent/"manual_handoff.json").write_text(json.dumps(handoff,ensure_ascii=False,indent=2),encoding="utf-8")
    result_path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8"); return data
if __name__=="__main__":mcp.run()
