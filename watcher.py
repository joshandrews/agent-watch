# Watches the sessions listed in sessions.json (same rules as the dashboard). When one needs the user (question dialog, waiting on them, stalled,
# or closed) it first prints a TRIAGE line so the coordinator session can resolve it itself (see coord.py);
# if the coordinator hasn't decided within TRIAGE_TIMEOUT, or decides to escalate, it phones the user via
# OpenAI Realtime (voice/realtime_bridge.py) + Telnyx, or ElevenLabs when the bridge is down or CALL_ENGINE=elevenlabs, and delivers each answer to the agent through Paseo (paseo send / paseo permit). stdout is the event stream (one line = one event); details go to watcher.log.
#   python3 watcher.py            run forever
#   python3 watcher.py --test     place one test call now and exit
import sys,os,json,time,glob,importlib,hashlib,calendar,urllib.request,urllib.error
HERE=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,os.path.join(HERE,'dashboard'))
import lib,config
ENV=config.ENV
TO=config.TO_NUMBER
USER=config.USER_NAME
STATE=f'{HERE}/state.json'
LOG=f'{HERE}/watcher.log'
POLL=60
CONFIRM=2            # consecutive polls a session must need the user before we call (avoids flaps)
RETRY_AFTER=15*60    # unanswered / unresolved items get re-called after this
MAX_TRIES=3          # per item, then give up until the item changes
SAME_ASK_H=6         # a reworded summary of the same kind of ask within this many hours counts as the same item

def same_ask(prev,it,now=None):
    """Is this the item we already called about? Same fingerprint, or the same kind of ask for the same agent whose
    wording the summariser changed (e.g. 'Pick an icon set' -> 'Pick one or two icon sets') within SAME_ASK_H."""
    if not prev:return False
    if prev.get('fp')==it['fp']:return True
    now=now or time.time()
    return (prev.get('fp') or '').split(':')[0]==it['fp'].split(':')[0] and now-prev.get('last',0)<SAME_ASK_H*3600 and 'tries' in prev
LIMIT_NUDGED={}            # session id -> when we last told it to carry on after a usage limit
ENGAGED_S=30*60       # no calls about an agent the user messaged this recently
TRIAGE_TIMEOUT=5*60  # coordinator gets this long to resolve an item before we phone anyway
QUIET=f'{HERE}/quiet_until'     # epoch seconds; no calls before then (coord.py quiet <minutes>)
OVERRIDE=f'{HERE}/quiet_override_until'   # epoch seconds; calls allowed despite quiet hours until then ("Resume calls")
NEEDS=f'{HERE}/needs.json'                # what the Paseo plugin shows as needing the user, with when a call would go out
CALL_STATUS=f'{HERE}/call_status.json'    # what the Paseo plugin shows: {pausedUntil, reason}
DECISIONS=f'{HERE}/decisions'   # decisions/<sid>.json written by coord.py: {fp, action: resolved|call, note}
SUMS=lib.SUMS
# How calls are placed. auto (default): an external calling CLI if one is configured (CALL_CLI, e.g. your team's
# internal tooling) and it accepts the call, otherwise your own keys: the OpenAI realtime bridge (OPENAI_API_KEY),
# then ElevenLabs (ELEVENLABS_API_KEY + EL_AGENT_ID). Or force one: cli | realtime | elevenlabs.
ENGINE=ENV.get('CALL_ENGINE','auto')
CLI_UNAVAILABLE_UNTIL=[0.0]              # after the calling CLI says no (missing, no access), use local keys for a while
BRIDGE='http://127.0.0.1:8766'

def log(*a):
    with open(LOG,'a') as f:print(time.strftime('%H:%M:%S'),*a,file=f)
def emit(s):print(s,flush=True);log('EVENT',s)
def el(method,path,body=None):
    req=urllib.request.Request('https://api.elevenlabs.io'+path,json.dumps(body).encode() if body else None,
        {'xi-api-key':ENV['ELEVENLABS_API_KEY'],'Content-Type':'application/json'},method=method)
    return json.load(urllib.request.urlopen(req,timeout=30))
def bridge(method,path,body=None):
    req=urllib.request.Request(BRIDGE+path,json.dumps(body).encode() if body else None,{'Content-Type':'application/json'},method=method)
    return json.load(urllib.request.urlopen(req,timeout=30))
class CallMayHaveStarted(RuntimeError):
    """The calling CLI couldn't say whether the call went out (its 'unknown outcome'): never dial again elsewhere."""

def call_cli(op,inp,timeout=60,key=None):
    """Runs one op of the external calling CLI: `<CALL_CLI> <op> --input '<json>' --json --no-input`, which must print
    {"ok": true, "data": {...}} (or {"ok": false, "error": {"code", "message"}} with a non-zero exit). Returns data."""
    import shlex,shutil,subprocess
    cmd=shlex.split(ENV.get('CALL_CLI',''))
    if not cmd or not shutil.which(cmd[0]):raise FileNotFoundError('no calling CLI installed')
    extra=['--idempotency-key',key] if key else []
    r=subprocess.run(cmd+[op,'--input',json.dumps(inp),'--json','--no-input']+extra,capture_output=True,text=True,timeout=timeout)
    try:out=json.loads(r.stdout or '{}')
    except Exception:out={}
    if r.returncode!=0 or not out.get('ok'):
        err=(out.get('error') or {})
        msg=f"{op}: {err.get('code') or r.returncode} {err.get('message') or r.stderr.strip()[:200]}"
        if err.get('code')=='unknown_outcome':raise CallMayHaveStarted(msg)
        raise RuntimeError(msg)
    return out.get('data') or {}

CLI_PLACE=ENV.get('CALL_CLI_PLACE','calls.place')   # op that places a call: {agentId, toNumber, dynamicVariables} -> {conversationId}
CLI_GET=ENV.get('CALL_CLI_GET','calls.get')         # op that reads it: {conversationId} -> {done, transcript[], dataCollection{}}

def cli_ready():
    """Use the calling CLI? Only when one is configured and installed, an agent is set, and it hasn't refused recently."""
    import shlex,shutil
    cmd=shlex.split(ENV.get('CALL_CLI',''))
    if ENGINE not in ('auto','cli') or not cmd or time.time()<CLI_UNAVAILABLE_UNTIL[0]:return False
    return bool(shutil.which(cmd[0])) and bool(cli_agent())

def cli_agent():
    """The voice agent the calling CLI should use: CALL_CLI_AGENT_ID, else EL_AGENT_ID."""
    return ENV.get('CALL_CLI_AGENT_ID') or ENV.get('EL_AGENT_ID')

def local_engine():
    """The best call path from local keys, or None when there is no way to place a call."""
    if ENGINE in ('auto','realtime') and ENV.get('OPENAI_API_KEY') and bridge_up():return 'realtime'
    if ENGINE!='cli' and ENV.get('ELEVENLABS_API_KEY') and ENV.get('EL_AGENT_ID') and ENV.get('EL_PHONE_ID'):return 'elevenlabs'
    return None

def bridge_up():
    try:return bool(bridge('GET','/health').get('ok'))
    except Exception:return False
def load_state():
    try:return json.load(open(STATE))
    except Exception:return {'items':{},'streak':{}}
def save_state(s):json.dump(s,open(STATE+'.tmp','w'),indent=1);os.replace(STATE+'.tmp',STATE)

import re
EXTERNAL_WAIT=lib.EXTERNAL_WAIT

def pending_question(sid):
    """The AskUserQuestion dialog a session is blocked on, if any: (tool_use_id, [questions])."""
    asks={};done=set()
    for line in lib.tail_lines(lib.transcript(sid),3_000_000):
        try:o=json.loads(line)
        except Exception:continue
        if o.get('isSidechain'):continue
        c=(o.get('message') or {}).get('content')
        if not isinstance(c,list):continue
        for b in c:
            if not isinstance(b,dict):continue
            if b.get('type')=='tool_use' and b.get('name')=='AskUserQuestion':asks[b['id']]=(b.get('input') or {}).get('questions') or []
            if b.get('type')=='tool_result':done.add(b.get('tool_use_id'))
    open_=[k for k in asks if k not in done]
    return (open_[-1],asks[open_[-1]]) if open_ else (None,None)

def describe_questions(qs):
    out=[]
    for q in qs:
        opts='; '.join(f"option {i}: {o.get('label')} ({o.get('description','')[:160]})" for i,o in enumerate(q.get('options') or [],1))
        out.append(f"It asked: \"{q.get('question')}\" Options: {opts}.")
    return ' '.join(out)

def needs():
    """Same state rules as agents-dashboard/gen.py. Returns {sid: item}."""
    global PENDING
    importlib.reload(lib);PENDING=lib.pending()
    try:SUM=json.load(open(SUMS))
    except Exception:SUM={}
    out={}
    for sid,(title,_,_) in lib.META.items():
        r=lib.parse(sid)
        if not r:continue
        sess=lib.SESS.get(sid)
        sm=SUM.get(sid) or {};pr=lib.progress(sid)
        parked=parked_since(sid)
        if parked and not any(t>parked for t,_ in r['users']):parked=True   # no new instruction since it was parked
        else:parked=False
        quiet=time.time()-r['mtime'];bg=lib.bg_active(sid)
        complete=bool(sm.get('complete')) or (pr and pr[0]>=pr[1] and quiet>300 and not bg)
        perms=PENDING.get((sess or {}).get('paseo_id'),[])
        ask=next((p for p in perms if p['name']=='AskUserQuestion'),None)
        perm=next((p for p in perms if p['name']!='AskUserQuestion'),None)
        qid=None;options=[]
        if not sess or sess.get('archived'):continue      # archived or deleted in Paseo: the user's deliberate choice
        # "call me now" (coord.py callnow, or the coordinator escalating something urgent) goes out even while the agent
        # is busy, until the user writes to it; the decision's own fingerprint makes the main loop dial at once
        u=urgent_call(sid)
        if u and not any(iso_epoch(t)>u['at'] and not from_me(m) for t,m in r['users']):
            out[sid]=dict(sid=sid,title=(sm.get('short_title') or '').strip() or title,qid=None,paseo_id=sess.get('paseo_id'),
                          name=sess.get('name') or '?',kind=u.get('kind') or 'waiting',
                          need=u.get('note') or sm.get('waiting_on_you') or 'It needs you now.',doing=sm.get('doing',''),fp=u['fp'],options=[])
            continue
        # the user has sent something newer than the agent's last reply: it has new work, whatever the summary says
        fresh_instruction=bool(r['users']) and (not r['asst'] or r['users'][-1][0]>r['asst'][-1][0])
        # stopped on a Claude usage limit: once it's been quiet a few minutes (the account switcher has usually moved on
        # to an account with quota), tell it to carry on. No call; this never needs the user.
        last=(r['asst'][-1][1] if r['asst'] else '')
        _pk=parked_since(sid)
        _parked=bool(_pk) and not any(t>_pk for t,_ in r['users'])
        if not fresh_instruction and not _parked and quiet>300 and re.search(r"hit your (session|weekly|usage) limit",last,re.I):
            # only once the active account has room again, and at most every 20 minutes per agent
            if quota_available() and time.time()-LIMIT_NUDGED.get(sid,0)>=1200:
                LIMIT_NUDGED[sid]=time.time()
                try:
                    deliver(dict(paseo_id=sess['paseo_id'],kind='message'),"You stopped on a usage limit. The account has switched or the limit has reset, so please carry on from where you left off.",who="Coordinator")
                    emit(f"RESUMED {sess['name']} after a usage limit")
                except Exception as e:log('limit nudge failed',repr(e))
            continue
        if sess.get('status') not in('running','idle'):
            kind,need='closed',f"Paseo reports it as {sess.get('status')}, so it is not working."
        elif ask:
            qid=ask['id'];qs=pending_question(sid)[1]
            kind,need='question',describe_questions(qs) if qs else f"It asked: {ask.get('description','')}"
            options=[o.get('label') for q in (qs or [])[:1] for o in q.get('options') or []]
        elif perm:
            qid=perm['id']
            options=['Allow','Deny']
            kind,need='permission',f"It is waiting for approval to use {perm['name']}: {tool_detail(sid,perm['name']) or perm.get('description','')}"
        elif fresh_instruction:continue
        # the user wrote to this agent in the last half hour: they're in that chat already, so a call would interrupt them
        elif any(not from_me(m) and time.time()-iso_epoch(t)<ENGAGED_S for t,m in r['users'][-3:]):continue
        elif complete and parked:continue
        # on hold ("Later", or the coordinator knows the user is on it): no calls about it, finished or waiting,
        # until the user writes to it or the hold expires
        elif held_since(sid) and not any(t>held_since(sid) and not from_me(m) for t,m in r['users']):continue
        elif complete:
            if quiet<300 or bg or EXTERNAL_WAIT.search(sm.get('doing') or ''):continue   # "done" but still waiting on CI etc.
            kind,need='finished',f"It has finished. What it did: {'; '.join(sm.get('done') or [])}. What should it do next?"
        elif parked:continue          # told to stop: stay quiet about finished/idle until it gets new work
        # a decision the user owes stays owed even while the agent keeps busy with background retries
        elif sm.get('waiting_on_you') and quiet>180:kind,need='waiting',sm['waiting_on_you']
        # quiet but waiting on something external (CI, a merge queue, a deploy, a retry loop) isn't stuck
        elif quiet>900 and not bg and EXTERNAL_WAIT.search(sm.get('doing') or ''):continue
        elif quiet>900 and not bg:kind,need='stalled',f'It has been idle for {lib.ago(r["last"]).replace(" ago","")}. Done so far: {"; ".join(sm.get("done") or [])}. Where it left off: {sm.get("doing","")}'
        else:continue
        # fingerprint: a new assistant message means a new situation worth a new call
        # waiting items are keyed by what is being asked, so new chatter from a busy agent doesn't restart the clock
        fp=f'{kind}:{qid}' if qid else f'waiting:{hashlib.sha1(need.encode()).hexdigest()[:10]}' if kind=='waiting' \
            else f'{kind}:{r["asst"][-1][0] if r["asst"] else r["last"]}'
        title=(sm.get('short_title') or '').strip() or title   # the summariser's short name reads better aloud
        out[sid]=dict(sid=sid,title=title,qid=qid,paseo_id=(sess or {}).get('paseo_id'),name=(sess or {}).get('name') or '?',
                      kind=kind,need=need,doing=sm.get('doing',''),fp=fp,options=options)
    return out

def call(items):
    lines=[]
    for i,it in enumerate(items,1):
        extra='' if it['kind']=='closed' else f" (Context: {it['doing']})" if it['doing'] else ''
        lines.append(f"{i}. {it['title']} [{it['kind']}]: {it['need']}{extra}")
    # the opening line reads the first item itself, so there's no silent gap waiting for the listener to speak
    first=items[0]
    intro=('One of your agents needs you. ' if len(items)==1 else f'{len(items)} of your agents need you. First, ')+f"{first['title']}. {first['need']}"
    variables={'items':'\n'.join(lines),'intro':intro,'user_name':USER}
    if cli_ready():
        import uuid
        key=str(uuid.uuid4())   # the same key on a retry can never dial twice
        try:
            try:r=call_cli(CLI_PLACE,{'agentId':cli_agent(),'toNumber':TO,'dynamicVariables':variables},key=key)
            except CallMayHaveStarted:
                time.sleep(5)
                r=call_cli(CLI_PLACE,{'agentId':cli_agent(),'toNumber':TO,'dynamicVariables':variables},key=key)
            log('CALL cli',json.dumps(r),'\n'+'\n'.join(lines))
            return 'cli:'+r['conversationId']
        except CallMayHaveStarted as e:
            # the phone may be ringing already: don't call again through local keys; the item is retried later
            raise RuntimeError(f'calling CLI could not confirm the call ({e}); not dialling again now')
        except Exception as e:
            # no access, not signed in, or the service is down: use local keys, and don't retry the CLI for 30 min
            CLI_UNAVAILABLE_UNTIL[0]=time.time()+1800
            log('calling CLI failed, using local keys:',e)
    engine=local_engine() if ENGINE!='cli' else None
    if engine is None:raise RuntimeError('no way to place calls: configure CALL_CLI or set OPENAI_API_KEY / ELEVENLABS_API_KEY')
    if engine=='realtime':
        r=bridge('POST','/dial',{'mode':'coordinator','opening':f"Hi {USER}, it's your agent coordinator. {intro}",'items':'\n'.join(lines)})
        log('CALL realtime',json.dumps(r),'\n'+'\n'.join(lines))
        return 'rt:'+r['id']
    r=el('POST','/v1/convai/sip-trunk/outbound-call',{'agent_id':ENV['EL_AGENT_ID'],'agent_phone_number_id':ENV['EL_PHONE_ID'],
        'to_number':TO,'conversation_initiation_client_data':{'dynamic_variables':variables}})
    log('CALL',json.dumps(r),'\n'+'\n'.join(lines))
    cid=r.get('conversation_id')
    if not r.get('success',True) or not cid:raise RuntimeError(f'call failed: {r}')
    return cid

def wait_result(cid,timeout=900):
    t0=time.time()
    while time.time()-t0<timeout:
        time.sleep(10)
        if cid.startswith('cli:'):
            # the calling CLI's get op, reshaped to look like ElevenLabs' conversation so answers() reads it alike
            try:c=call_cli(CLI_GET,{'conversationId':cid[4:]})
            except Exception as e:log('poll err',e);continue
            if not c.get('done'):continue
            return {'status':c.get('status'),'transcript':[{'role':t.get('role'),'message':t.get('message')} for t in c.get('transcript') or []],
                    'analysis':{'data_collection_results':{k:{'value':v.get('value')} for k,v in (c.get('dataCollection') or {}).items()}}}
        if cid.startswith('rt:'):           # the bridge returns the same shape: transcript + answers
            try:c=bridge('GET','/result/'+cid[3:])
            except Exception as e:log('poll err',e);continue
            if c.get('status')=='done':return c
            continue
        try:c=el('GET',f'/v1/convai/conversations/{cid}')
        except Exception as e:log('poll err',e);continue
        if c.get('status') in ('done','failed'):
            if c.get('status')=='done' and not (c.get('analysis') or {}).get('data_collection_results'):
                time.sleep(5);c=el('GET',f'/v1/convai/conversations/{cid}')   # analysis lands just after
            return c
    return None

def answers(c):
    if isinstance((c or {}).get('answers'),list):return [x for x in c['answers'] if isinstance(x,dict)]
    v=(((c or {}).get('analysis') or {}).get('data_collection_results') or {}).get('answers',{}).get('value')
    if not v:return []
    try:
        a=json.loads(v[v.index('['):v.rindex(']')+1]);return [x for x in a if isinstance(x,dict)]
    except Exception:log('bad answers',v);return []

def tool_detail(sid,tool):
    """What the agent is asking to run: the input of its latest call to that tool."""
    for line in reversed(lib.tail_lines(lib.transcript(sid),2_000_000)):
        try:o=json.loads(line)
        except Exception:continue
        for b in reversed((o.get('message') or {}).get('content') or []) if isinstance((o.get('message') or {}).get('content'),list) else []:
            if isinstance(b,dict) and b.get('type')=='tool_use' and b.get('name')==tool:
                i=b.get('input') or {};return (i.get('description') or '')+' '+str(i.get('command') or i.get('file_path') or i)[:300]
    return ''

def paseo(*args):
    import subprocess
    r=subprocess.run(['paseo',*args],capture_output=True,text=True,timeout=60)
    if r.returncode:raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout.strip()

def deliver(it,instruction,who=None):
    """Hand the user's answer to the agent through Paseo. Returns what was done."""
    who=who or f'{USER}, by phone'
    pid,kind=it['paseo_id'],it['kind']
    if not pid:raise RuntimeError('agent is not in Paseo')
    if kind in('question','permission'):
        still=[p['id'] for p in lib.pending().get(pid,[])]
        if it['qid'] not in still:return 'already answered in Paseo'
        if kind=='permission' and instruction.strip().lower().startswith(('allow','approve','yes')):
            paseo('permit','allow',pid,it['qid']);return 'approved the request'
        # a denied AskUserQuestion carries the answer back to the agent as its result
        paseo('permit','deny',pid,it['qid'],'--message',f'({who}) {instruction}')
        return 'answered the question' if kind=='question' else 'denied the request, with the reason'
    paseo('send','--no-wait',pid,f'({who}) {instruction}')
    return 'sent as a message'

def parked_since(sid):
    """ISO time the session was parked (told to stop), or None. decisions/<sid>.park is written by coord.py park."""
    try:return open(f'{DECISIONS}/{sid}.park').read().strip()
    except Exception:return None

def _num(path):
    try:return float(open(path).read())
    except Exception:return 0

def paused_until(now):
    """(epoch the pause ends, 'manual'|'hours') or (0, None). Also records it for the Paseo plugin."""
    manual=_num(QUIET);hours=config.quiet_hours_end(now)
    if hours and _num(OVERRIDE)>now:hours=None
    until,reason=max((manual,'manual') if manual>now else (0,None),(hours,'hours') if hours else (0,None))
    try:json.dump(dict(pausedUntil=until or None,reason=reason,quietHours=config.QUIET_HOURS or None),open(CALL_STATUS,'w'))
    except Exception:pass
    return until,reason

def write_needs(cur,st,now,paused):
    """Record each open item and roughly when the watcher would phone about it, for the Paseo plugin."""
    out=[]
    for sid,it in cur.items():
        prev=st['items'].get(sid) or {}
        if prev.get('fp')==it['fp'] and prev.get('resolved'):continue
        d={}
        if same_ask(prev,it) and prev.get('tries',0)>=MAX_TRIES:call_at=None
        else:
            d=decision(sid,it['fp']);t=(st.get('triage') or {}).get(sid) or {}
            if d.get('action')=='call':call_at=now+POLL
            elif t.get('fp')==it['fp']:call_at=t['at']+TRIAGE_TIMEOUT
            else:call_at=now+max(0,CONFIRM-(st['streak'].get(sid) or {}).get('n',0))*POLL+TRIAGE_TIMEOUT
            if prev.get('fp')==it['fp'] and prev.get('last'):call_at=max(call_at,prev['last']+RETRY_AFTER)
            if paused and not d.get('now'):call_at=max(call_at,paused)
        out.append(dict({k:it[k] for k in ('sid','name','title','kind','need','options','paseo_id')},callAt=call_at,held=bool(paused) and not d.get('now')))
    try:json.dump(out,open(NEEDS+'.tmp','w'));os.replace(NEEDS+'.tmp',NEEDS)
    except Exception as e:log('needs.json',repr(e))

def quota_available(max_pct=90):
    """True if the active Claude account (per cswap) has 5-hour and weekly quota left; True if cswap isn't installed."""
    import subprocess
    try:out=subprocess.run(['cswap','status'],capture_output=True,text=True,timeout=20).stdout
    except Exception:return True
    five=re.search(r"5h:\s+(\d+)%",out);week=re.search(r"7d:\s+(\d+)%",out)
    return (not five or int(five.group(1))<max_pct) and (not week or int(week.group(1))<98)

def iso_epoch(t):
    try:import calendar;return calendar.timegm(time.strptime(str(t)[:19],'%Y-%m-%dT%H:%M:%S'))
    except Exception:return 0

def from_me(m):
    """A 'user' message that wasn't the user: one of mine, or a context-compaction continuation."""
    m=str(m).lstrip();return m.startswith('(') and 'coordinator' in m[:40].lower() or m.startswith('This session is being continued')

HOLD_HOURS=float(ENV.get('HOLD_HOURS',3))   # a hold means "not now", not "never": after this, one more call

def urgent_call(sid,max_age=3600):
    """An explicit call-now decision from the last hour, or None."""
    try:d=json.load(open(f'{DECISIONS}/{sid}.json'))
    except Exception:return None
    if d.get('action')=='call' and d.get('now') and time.time()-d.get('at',0)<max_age:return d
    return None

def held_since(sid):
    """ISO time an agent's open question was put on the user's list (coord.py hold), or None once it has expired."""
    f=f'{DECISIONS}/{sid}.hold'
    try:at=open(f).read().strip()
    except Exception:return None
    try:
        age=time.time()-calendar.timegm(time.strptime(at[:19],'%Y-%m-%dT%H:%M:%S'))
        if age>HOLD_HOURS*3600:
            os.remove(f);log('HOLD expired',sid[:8]);return None
    except Exception:pass
    return at

def decision(sid,fp):
    try:d=json.load(open(f'{DECISIONS}/{sid}.json'))
    except Exception:return {}
    return d if d.get('fp')==fp else {}

def transcript(c):
    return '\n'.join(f"{m.get('role')}: {m.get('message')}" for m in (c or {}).get('transcript',[]) if m.get('message'))

def run_call(items,st):
    if not TO:   # no phone set up: everything else still works, the items just stay on the menu bar and dashboard
        log('no TO_NUMBER; not calling about',', '.join(i['name'] for i in items));return
    try:cid=call(items)
    except Exception as e:
        body=e.read().decode() if isinstance(e,urllib.error.HTTPError) else str(e)
        emit(f'ERROR call could not be placed: {body[:300]}');return
    emit(f'CALLING {TO} about: '+'; '.join(f"{it['name']} ({it['kind']})" for it in items)+f' [conversation {cid}]')
    c=wait_result(cid)
    ans=answers(c);got=set()
    for a in ans:
        try:it=items[int(a['item'])-1]
        except Exception:continue
        got.add(it['sid'])
        st['items'][it['sid']]=dict(fp=it['fp'],resolved=True,at=time.time())
        if a.get('skip'):emit(f"SKIPPED {it['name']} | {it['title']}");continue
        try:emit(f"DELIVERED {it['name']} ({it['title']}): {deliver(it,a.get('instruction',''))}: {a.get('instruction','')}");continue
        except Exception as e:log('delivery failed',repr(e))
        emit('RELAY '+json.dumps(dict(to=it['name'],paseo_id=it['paseo_id'],title=it['title'],kind=it['kind'],instruction=a.get('instruction',''))))
    open(f'{HERE}/calls/{cid}.txt','w').write(transcript(c)) if c else None
    missed=[it for it in items if it['sid'] not in got]
    if missed:
        last=[i['name'] for i in missed if (st['items'].get(i['sid']) or {}).get('tries',0)>=MAX_TRIES]
        more=[i['name'] for i in missed if i['name'] not in last]
        emit(f"NO-ANSWER for {', '.join(i['name'] for i in missed)} (call status {(c or {}).get('status','timeout')}; transcript calls/{cid}.txt); "
             +'; '.join(([f"will retry {', '.join(more)} in {RETRY_AFTER//60} min"] if more else [])
                        +([f"no more calls about {', '.join(last)} (tried {MAX_TRIES} times; still in Needs you)"] if last else [])))
    save_state(st)

def main():
    os.makedirs(f'{HERE}/calls',exist_ok=True)
    if '--test' in sys.argv:
        run_call([dict(sid='test',title='Test agent',name='test',kind='waiting',fp='test',
                       need=f'This is a test of the phone line. Ask {USER} to say any instruction, e.g. "tell it to carry on".',doing='')],load_state());return
    st=load_state()
    how='the calling CLI' if cli_ready() else (local_engine() or 'nothing: no calling CLI, no OpenAI or ElevenLabs keys')
    emit(f'WATCHING {len(lib.META)} sessions; '+(f'will call {TO} via {how} when one needs you' if TO else 'no TO_NUMBER, so no phone calls'))
    last_res=0
    while True:
        try:
            if time.time()-last_res>=600:     # every 10 min: CPU, memory, disk, idle dev stacks
                last_res=time.time()
                import threading,resources
                def _res():
                    try:
                        for a in resources.run()['actions']:emit('RESOURCES '+a)
                    except Exception as e:log('resources error',repr(e))
                threading.Thread(target=_res,daemon=True).start()
            cur=needs();now=time.time()
            for sid in list(st['streak']):
                if sid not in cur:st['streak'].pop(sid)
            due=[]
            for sid,it in cur.items():
                s=st['streak'].get(sid) or {'fp':it['fp'],'n':0}
                s={'fp':it['fp'],'n':s['n']+1 if s['fp']==it['fp'] else 1};st['streak'][sid]=s
                prev=st['items'].get(sid) or {}
                if not same_ask(prev,it,now):prev={}
                if s['n']<CONFIRM or (prev.get('resolved') and prev.get('fp')==it['fp']):continue
                d=decision(sid,it['fp'])
                # an explicit "call me now" skips the retry wait and the retry limit
                if not (d.get('action')=='call' and d.get('now') and d.get('at',0)>prev.get('last',0)):
                    if prev.get('tries',0)>=MAX_TRIES or now-prev.get('last',0)<RETRY_AFTER:continue
                if d.get('action')=='resolved':
                    st['items'][sid]=dict(fp=it['fp'],resolved=True,at=now,by='coordinator');continue
                if d.get('action')=='call':
                    # the coordinator's read replaces the raw description, so the call says one clear thing
                    if d.get('note'):it['need']=d['note'];it['doing']=''
                    if d.get('kind'):it['kind']=d['kind']
                else:
                    t=st.setdefault('triage',{}).get(sid) or {}
                    if t.get('fp')!=it['fp']:
                        st['triage'][sid]=dict(fp=it['fp'],at=now)
                        emit('TRIAGE '+json.dumps({k:it[k] for k in ('name','sid','title','kind','need','doing')}))
                        continue
                    if now-t['at']<TRIAGE_TIMEOUT:continue
                    log('triage timed out; calling',sid)
                due.append(it)
            quiet,reason=paused_until(now)
            write_needs(cur,st,now,quiet)
            if due and quiet:
                # "Call me now" from the plugin goes out regardless; everything else waits
                urgent=[i for i in due if decision(i['sid'],i['fp']).get('now')]
                held=[i for i in due if i not in urgent]
                if held and st.get('held')!=sorted(i['sid'] for i in held):
                    st['held']=sorted(i['sid'] for i in held)
                    emit(f"HELD (do not disturb until {time.strftime('%H:%M',time.localtime(quiet))}): "+'; '.join(f"{i['name']} ({i['kind']})" for i in held))
                due=urgent
            if due:
                st.pop('held',None)
                for it in due:
                    p=st['items'].get(it['sid']) or {}
                    st['items'][it['sid']]=dict(fp=it['fp'],tries=(p.get('tries',0) if same_ask(p,it,now) else 0)+1,last=now)
                save_state(st);run_call(due,st)
            save_state(st)
        except Exception as e:log('loop error',repr(e))
        time.sleep(POLL)
if __name__=='__main__':main()
