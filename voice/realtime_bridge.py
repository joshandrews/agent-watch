# Phone calls on OpenAI's realtime speech-to-speech model: Telnyx streams the call audio (8 kHz u-law) over a WebSocket,
# this bridge passes it straight to OpenAI Realtime and plays the model's audio back. The model hears tone and timing
# directly; semantic VAD finds the caller's turns. We decide interruptions: the agent stops only if the caller keeps talking
# ~0.4 s over it (noise and "mm-hmm" don't), and then queued audio is cleared and the model told what was actually heard.
# Run:  voice/.venv/bin/python voice/realtime_bridge.py     (starts a Cloudflare quick tunnel for Telnyx)
#   POST /dial {"mode": "coordinator"|"briefing", "opening": "...", "items": "..."|"script": "...", "facts": "..."} -> {"id"}
#   GET  /result/<id> -> {"status": "in-progress"|"done", "transcript": [...], "answers": [...]}
import asyncio, audioop, base64, json, os, re, subprocess, sys, time, uuid, urllib.request
import aiohttp
from aiohttp import web, WSMsgType

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import config                                   # noqa: E402

PORT = 8766
MODEL = config.ENV.get("REALTIME_MODEL", "gpt-realtime-2.1")
VOICE = config.ENV.get("REALTIME_VOICE", "marin")
LOG = os.path.join(HERE, "bridge.log")
SESSIONS, STATE = {}, {"tunnel": None, "reachable": False, "proc": None}
ULAW_BYTES_PER_MS = 8                           # 8 kHz, one byte per sample
BARGE_IN_S = 0.4         # the caller must keep talking this long over the agent before it stops (noise and "mm-hmm" don't)
DEBUG = bool(config.ENV.get("VOICE_DEBUG") or os.environ.get("VOICE_DEBUG"))
MIN_TURN_MS = 250
QUIET_RMS = 350          # u-law decoded level below which the caller counts as quiet (phone hiss sits under this)
FALLBACK_SILENCE_S = 0.9 # if OpenAI hasn't ended the caller's turn after this much quiet, we end it ourselves        # blips shorter than this, when the agent is quiet, aren't a turn

STYLE = """How to talk (a live phone call):
- Sound like a relaxed, sharp colleague. Contractions, plain words, natural rhythm. Never formal, never a list.
- Keep turns short: usually one sentence. Never restate their whole answer; confirm in a few words ("Got it, watch production.") and move on.
- "Mm-hmm" while you talk means keep going. "Yes", "yeah", "okay" right after your question is the answer: take it. Never ask the same thing twice.
- If they say "hang on", or talk to someone else in the room, just wait silently until they come back.
- If something's unclear, ask one short question. Never guess.
- Say numbers like a person would. Never read IDs, links or PR numbers unless asked."""


def coordinator_instructions(items):
    return f"""You are {config.USER_NAME}'s agent coordinator, phoning because some of their AI agents need them.
Items, in order (item 1 is read out in your opening line):
{items}

Go through the items one at a time. For each, get an answer or a "skip", call record_answer silently, then move on with a short lead-in that names that item's own agent. Read each later item in at most two short sentences.
Never say what you're doing behind the scenes (no "let me capture/record/note/pass that on/line that up"). Just confirm in a few words and move on.
When every item is covered, say a quick goodbye and call end_call.
If a voicemail greeting or recorded message answers, don't record any answers: call end_call straight away, without leaving a message.
{STYLE}"""


def briefing_instructions(script, facts):
    return f"""You just read {config.USER_NAME} this briefing word for word. Now answer their questions about it briefly, using only the briefing and facts. When they're done, say goodbye and call end_call.
BRIEFING:
{script}
FACTS:
{facts}
{STYLE}"""


TOOLS = [
    {"type": "function", "name": "record_answer",
     "description": "Record the user's answer to one item as soon as they give it.",
     "parameters": {"type": "object", "properties": {
         "item": {"type": "integer", "description": "Item number"},
         "instruction": {"type": "string", "description": "Their answer, rewritten as a clear instruction to that agent, in their words where possible"},
         "skip": {"type": "boolean", "description": "True if they chose to skip it"}},
         "required": ["item", "instruction", "skip"]}},
    {"type": "function", "name": "end_call", "description": "Hang up after your goodbye has been said.",
     "parameters": {"type": "object", "properties": {}}},
]


def log(*a):
    with open(LOG, "a") as f:
        print(time.strftime("%H:%M:%S"), *a, file=f)


def telnyx(method, path, body=None):
    req = urllib.request.Request("https://api.telnyx.com/v2" + path, json.dumps(body).encode() if body is not None else None,
                                 {"Authorization": f"Bearer {config.ENV['TELNYX_API_KEY']}", "Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


class Call:
    def __init__(self, sid, mode, instructions, opening):
        self.sid, self.mode, self.instructions, self.opening = sid, mode, instructions, opening
        self.call_control_id = None
        self.tel = None                 # Telnyx media WebSocket
        self.oai = None                 # OpenAI realtime WebSocket
        self.status = "dialing"
        self.created = time.time()
        self.transcript, self.answers = [], []
        self.item_id, self.item_sent_ms, self.item_started = None, 0, 0.0   # the agent audio item being played
        self.play_until = 0.0
        self.ending = False
        self.opening_done = False
        self.responding = False          # a model response is in progress
        self.speech_start_ms = None      # caller speech currently in progress (input buffer time)
        self.barge_task = None
        self.interrupted_now = False
        self.quiet_since = None          # when the caller's line last went quiet (our own fallback end-of-turn)
        self.voicemail = False           # reached voicemail: no answers count

    async def send_oai(self, ev):
        await self.oai.send_json(ev)

    async def start(self):
        s = aiohttp.ClientSession()
        self.oai = await s.ws_connect(f"wss://api.openai.com/v1/realtime?model={MODEL}",
                                      headers={"Authorization": f"Bearer {config.ENV['OPENAI_API_KEY']}"}, heartbeat=20)
        self._http = s
        briefing = self.mode == "briefing"
        await self.send_oai({"type": "session.update", "session": {
            "type": "realtime", "model": MODEL, "instructions": self.instructions,
            "tools": TOOLS if not briefing else [TOOLS[1]], "tool_choice": "auto",
            "audio": {
                "input": {"format": {"type": "audio/pcmu"},
                          "noise_reduction": {"type": "near_field"},
                          "transcription": {"model": "gpt-realtime-whisper", "language": "en"},
                          # we create responses and handle interruptions ourselves (see on_speech_stopped / barge_in_after)
                          "turn_detection": {"type": "semantic_vad", "eagerness": "high", "create_response": False,
                                             "interrupt_response": False}},
                "output": {"format": {"type": "audio/pcmu"}, "voice": VOICE}}}})
        say = "Say exactly this, naturally, then stop and listen" if not briefing else "Read this word for word, naturally paced, then ask if they have questions"
        await self.send_oai({"type": "response.create", "response": {"instructions": f"{say}:\n{self.opening}"}})
        asyncio.create_task(self.pump_oai())

    async def pump_oai(self):
        async for msg in self.oai:
            if msg.type != WSMsgType.TEXT:
                continue
            ev = json.loads(msg.data)
            t = ev.get("type", "")
            if DEBUG and not t.startswith(("response.output_audio", "conversation.item.input_audio_transcription.delta")):
                log(self.sid, "ev", t, json.dumps({k: ev[k] for k in ("audio_start_ms", "audio_end_ms", "item_id") if k in ev}))
            if t == "response.output_audio.delta":
                if ev["item_id"] != self.item_id:
                    self.item_id, self.item_sent_ms, self.item_started = ev["item_id"], 0, time.time()
                b = base64.b64decode(ev["delta"])
                self.item_sent_ms += len(b) // ULAW_BYTES_PER_MS
                self.play_until = max(self.play_until, time.time()) + len(b) / (ULAW_BYTES_PER_MS * 1000)
                if self.tel is not None and not self.tel.closed:
                    await self.tel.send_json({"event": "media", "media": {"payload": ev["delta"]}})
            elif t == "response.created":
                self.responding = True
            elif t == "input_audio_buffer.speech_started":
                self.speech_start_ms, self.interrupted_now = ev.get("audio_start_ms", 0), False
                if self.agent_audible() and self.barge_task is None:
                    self.barge_task = asyncio.create_task(self.barge_in_after(BARGE_IN_S))
            elif t == "input_audio_buffer.speech_stopped":
                await self.on_speech_stopped(ev.get("audio_end_ms", 0))
            elif t == "response.output_audio_transcript.done":
                self.transcript.append({"role": "agent", "message": ev.get("transcript", "")})
                log(self.sid, "agent:", ev.get("transcript", ""))
            elif t == "conversation.item.input_audio_transcription.completed":
                self.transcript.append({"role": "user", "message": ev.get("transcript", "").strip()})
                log(self.sid, "user:", ev.get("transcript", "").strip())
            elif t == "response.done":
                await self.on_response_done(ev.get("response", {}))
            elif t == "error":
                log(self.sid, "openai error:", json.dumps(ev.get("error"))[:300])
        log(self.sid, "openai socket closed")

    async def fallback_turn(self, payload):
        """Safety net: semantic VAD sometimes waits seconds (or forever, on a hissy line) to end a turn.
        If the caller has been quiet for FALLBACK_SILENCE_S mid-turn, commit the audio and reply ourselves."""
        if self.speech_start_ms is None:
            self.quiet_since = None
            return
        rms = audioop.rms(audioop.ulaw2lin(base64.b64decode(payload), 2), 2)
        now = time.time()
        if rms > QUIET_RMS:
            self.quiet_since = None
        elif self.quiet_since is None:
            self.quiet_since = now
        elif now - self.quiet_since >= FALLBACK_SILENCE_S and not self.agent_audible():
            log(self.sid, "fallback end of turn")
            self.speech_start_ms, self.quiet_since = None, None
            await self.send_oai({"type": "input_audio_buffer.commit"})
            if self.responding:
                await self.send_oai({"type": "response.cancel"})
            await self.send_oai({"type": "response.create"})

    def agent_audible(self):
        return time.time() < self.play_until

    async def barge_in_after(self, delay):
        """Stop the agent only if the caller is still talking after `delay` (a real interruption, not a cough)."""
        await asyncio.sleep(delay)
        self.barge_task = None
        if self.speech_start_ms is None or not self.agent_audible():
            return
        if self.mode == "briefing" and not self.opening_done:
            return                                         # the briefing itself isn't interruptible
        heard_ms = int((time.time() - self.item_started) * 1000)
        if self.responding:
            await self.send_oai({"type": "response.cancel"})
        if self.tel is not None and not self.tel.closed:
            await self.tel.send_json({"event": "clear"})
        self.play_until, self.interrupted_now = 0.0, True
        if self.item_id:
            await self.send_oai({"type": "conversation.item.truncate", "item_id": self.item_id, "content_index": 0,
                                 "audio_end_ms": max(0, min(heard_ms, self.item_sent_ms))})
        log(self.sid, f"interrupted after {heard_ms}ms")

    async def on_speech_stopped(self, end_ms):
        """The caller paused. Decide whether that was a turn that deserves a reply."""
        if self.speech_start_ms is None:
            return                                         # already handled by the fallback
        dur = end_ms - self.speech_start_ms
        self.speech_start_ms = None
        if self.barge_task:
            self.barge_task.cancel()
            self.barge_task = None
        if self.agent_audible() and not self.interrupted_now:
            log(self.sid, f"ignored {dur}ms while speaking (backchannel/noise)")
            return                                         # "mm-hmm" or noise over the agent: keep talking
        if dur < MIN_TURN_MS:
            log(self.sid, f"ignored {dur}ms blip")
            return
        if self.responding:
            await self.send_oai({"type": "response.cancel"})
        await self.send_oai({"type": "response.create"})

    async def on_response_done(self, resp):
        self.responding = False
        if not self.opening_done:
            self.opening_done = True
        calls = [o for o in resp.get("output", []) if o.get("type") == "function_call"]
        for c in calls:
            args = json.loads(c.get("arguments") or "{}")
            if c["name"] == "record_answer" and not self.voicemail:
                self.answers = [a for a in self.answers if a.get("item") != args.get("item")] + [args]
                log(self.sid, "answer:", json.dumps(args))
            elif c["name"] == "end_call":
                self.ending = True
            if self.transcript and "voicemail" in self.transcript[-1]["message"].lower() and self.transcript[-1]["role"] == "user":
                self.voicemail = True
            await self.send_oai({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": c["call_id"], "output": json.dumps({"ok": True})}})
        if self.ending:
            await asyncio.sleep(max(0.0, self.play_until - time.time()) + 0.6)
            self.hangup()
        elif calls:
            await self.send_oai({"type": "response.create"})   # carry on talking after recording the answer

    def hangup(self):
        try:
            telnyx("POST", f"/calls/{self.call_control_id}/actions/hangup", {})
        except Exception as e:
            log(self.sid, "hangup:", e)

    async def finish(self):
        if self.status == "done":
            return
        self.status = "done"
        if self.voicemail:
            self.answers = []
        for ws in (self.oai,):
            if ws is not None and not ws.closed:
                await ws.close()
        if getattr(self, "_http", None):
            await self._http.close()
        log(self.sid, "done; answers:", json.dumps(self.answers))


async def media(request):
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    call = SESSIONS.get(request.query.get("s", ""))
    if call is None:
        await ws.close()
        return ws
    call.tel, call.status = ws, "in-progress"
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        m = json.loads(msg.data)
        ev = m.get("event")
        if ev == "start":
            call.call_control_id = m["start"].get("call_control_id") or call.call_control_id
            log(call.sid, "stream start", json.dumps(m["start"].get("media_format")))
            await call.start()
        elif ev == "media" and call.oai is not None and m["media"].get("track", "inbound") == "inbound":
            await call.send_oai({"type": "input_audio_buffer.append", "audio": m["media"]["payload"]})
            await call.fallback_turn(m["media"]["payload"])
        elif ev == "stop":
            break
    await call.finish()
    return ws


async def webhook(request):
    d = (await request.json()).get("data", {})
    sid = (d.get("payload") or {}).get("client_state")
    sid = base64.b64decode(sid).decode() if sid else ""
    log("webhook", d.get("event_type"), sid)
    if d.get("event_type") == "call.hangup" and sid in SESSIONS:
        asyncio.create_task(SESSIONS[sid].finish())
    elif d.get("event_type") == "call.machine.detection.ended" and sid in SESSIONS:
        res = (d.get("payload") or {}).get("result")
        log(sid, "answering machine detection:", res)
        if res in ("machine", "fax"):            # voicemail: hang up at once, and record nothing
            SESSIONS[sid].voicemail = True
            SESSIONS[sid].hangup()
    return web.json_response({})


async def dial(request):
    b = await request.json()
    sid = uuid.uuid4().hex[:12]
    mode = b.get("mode", "coordinator")
    instr = briefing_instructions(b.get("script", ""), b.get("facts", "")) if mode == "briefing" else coordinator_instructions(b.get("items", ""))
    call = SESSIONS[sid] = Call(sid, mode, instr, b["opening"])
    host = STATE["tunnel"].replace("https://", "")
    r = telnyx("POST", "/calls", {
        "connection_id": config.ENV["TELNYX_CC_APP_ID"], "to": b.get("to") or config.TO_NUMBER, "from": config.ENV["TELNYX_NUMBER"],
        "webhook_url": f"{STATE['tunnel']}/telnyx", "client_state": base64.b64encode(sid.encode()).decode(),
        "stream_url": f"wss://{host}/media?s={sid}", "stream_track": "inbound_track", "stream_codec": "PCMU",
        "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU", "stream_bidirectional_sampling_rate": 8000,
        "answering_machine_detection": "detect",
    })
    call.call_control_id = r["data"]["call_control_id"]
    log(sid, "dialing", mode)
    return web.json_response({"id": sid})


async def result(request):
    call = SESSIONS.get(request.match_info["sid"])
    if call is None:
        raise web.HTTPNotFound()
    return web.json_response({"status": call.status, "transcript": call.transcript, "answers": call.answers})


async def health(request):
    # ok only when Telnyx can actually reach us: a quick tunnel can die (e.g. after a network drop) while this
    # process keeps running, and calls placed then ring with no audio and no webhooks
    ok = STATE["tunnel"] is not None and STATE["reachable"]
    return web.json_response({"ok": ok, "tunnel": STATE["tunnel"], "reachable": STATE["reachable"], "model": MODEL})


async def watch_tunnel(app):
    """Checks the public tunnel every minute; after 3 failures in a row (and no call in progress) starts a new one."""
    async def loop():
        fails = 0
        async with aiohttp.ClientSession() as http:
            while True:
                try:
                    async with http.get(f"{STATE['tunnel']}/health", timeout=aiohttp.ClientTimeout(total=8)) as r:
                        STATE["reachable"] = r.status == 200
                except Exception:
                    STATE["reachable"] = False
                fails = 0 if STATE["reachable"] else fails + 1
                busy = any(c.status != "done" and time.time() - c.created < 1200 for c in SESSIONS.values())
                if fails >= 3 and not busy:
                    log("tunnel unreachable; starting a new one")
                    if STATE["proc"]:
                        STATE["proc"].terminate()
                    STATE["proc"] = await asyncio.get_running_loop().run_in_executor(None, start_tunnel)
                    fails = 0
                await asyncio.sleep(60 if STATE["reachable"] else 20)
    app["tunnel_watch"] = asyncio.create_task(loop())


def start_tunnel():
    p = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for line in p.stderr:
        # a quick tunnel is a random hyphenated name; skip Cloudflare's own hosts (e.g. api.trycloudflare.com in errors)
        m = re.search(r"https://(?!api\.)[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com", line)
        if m:
            STATE["tunnel"] = m.group(0)
            log("tunnel", m.group(0))
            break
    return p


def main():
    STATE["proc"] = start_tunnel()
    app = web.Application()
    app.on_startup.append(watch_tunnel)
    app.add_routes([web.get("/media", media), web.post("/telnyx", webhook), web.post("/dial", dial),
                    web.get("/result/{sid}", result), web.get("/health", health)])
    try:
        web.run_app(app, host="127.0.0.1", port=PORT, print=None)
    finally:
        if STATE["proc"]:
            STATE["proc"].terminate()


if __name__ == "__main__":
    main()
