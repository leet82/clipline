"""Clipline worker: Twitch top clips -> vertical captioned Shorts -> review queue.

Runs in GitHub Actions. Rendered files are stored as assets on the
`clip-store` GitHub release; data/queue.json tracks every clip seen.
"""
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
QUEUE = DATA / "queue.json"
DECISIONS = DATA / "decisions.json"
RELEASE_TAG = "clip-store"
WORK = pathlib.Path(os.environ.get("CLIPLINE_WORK", "/tmp/clipline"))
W, H = 1080, 1920


def log(*a):
    print("[clipline]", *a, flush=True)


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(p, obj):
    p.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")


def run(cmd, **kw):
    log("$", " ".join(str(c) for c in cmd)[:200])
    return subprocess.run(cmd, check=True, **kw)


# ---------------------------------------------------------------- Twitch
class Twitch:
    def __init__(self):
        self.cid = os.environ["TWITCH_CLIENT_ID"]
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": self.cid,
                "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        r.raise_for_status()
        self.token = r.json()["access_token"]

    def get(self, path, **params):
        r = requests.get(
            f"https://api.twitch.tv/helix/{path}",
            headers={"Client-Id": self.cid, "Authorization": f"Bearer {self.token}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["data"]

    def user_ids(self, logins):
        return {u["login"].lower(): u["id"] for u in self.get("users", login=logins)}

    def top_clips(self, broadcaster_id, hours):
        end = now()
        return self.get(
            "clips",
            broadcaster_id=broadcaster_id,
            started_at=iso(end - dt.timedelta(hours=hours)),
            ended_at=iso(end),
            first=50,
        )

    def clips_by_id(self, ids):
        return self.get("clips", id=ids[:100]) if ids else []

    def game_names(self, ids):
        ids = sorted({i for i in ids if i})
        return {g["id"]: g["name"] for g in self.get("games", id=ids[:100])} if ids else {}


# ---------------------------------------------------------------- media
def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    j = json.loads(out)
    s = j["streams"][0]
    return int(s["width"]), int(s["height"]), float(j["format"]["duration"])


_whisper = None


def transcribe(path):
    """Word-level timestamps via faster-whisper. Returns [(start, end, word)]."""
    global _whisper
    from faster_whisper import WhisperModel

    import numpy as np

    # Decode with ffmpeg ourselves: faster-whisper's PyAV decoder breaks on newer PyAV releases.
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True,
    ).stdout
    audio = np.frombuffer(raw, np.float32).copy()
    if audio.size < 16000:  # no audio track or under 1 s
        return []
    if _whisper is None:
        _whisper = WhisperModel(os.environ.get("WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
    segments, _ = _whisper.transcribe(audio, word_timestamps=True, vad_filter=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            t = w.word.strip()
            if t:
                words.append((w.start, w.end, t))
    return words


def _ass_time(t):
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


# Burned-in captions can't be edited after posting, so slurs are always masked and
# common swears softened (platforms down-rank captions with heavy profanity).
_MASK = [
    (re.compile(r"\bn+i+g+(?:g+)?(?:a+|e+r+|a+h+|a+z+|a+s+|e+r+s+)\b", re.I), "N****"),
    (re.compile(r"\bf+a+g+(?:g+o+t+)?s?\b", re.I), "F**"),
    (re.compile(r"\br+e+t+a+r+d+(?:e+d+|s+)?\b", re.I), "R*****"),
    (re.compile(r"f+u+c+k", re.I), "F*CK"),
    (re.compile(r"\bs+h+i+t", re.I), "SH*T"),
    (re.compile(r"\bb+i+t+c+h", re.I), "B*TCH"),
]


def clean_word(w):
    for rx, rep in _MASK:
        w = rx.sub(rep, w)
    return w.replace("{", "(").replace("}", ")").replace("\\", "")


def chunk_words(words, max_words=3, max_gap=0.6):
    chunks, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w[0] - cur[-1][1] > max_gap or re.search(r"[.!?]$", cur[-1][2])):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


HIGHLIGHT = "&H0000E5FF&"  # ASS is BGR: bright yellow for the word being spoken


def build_ass(words, path, cap_y, hook="", hook_y=330, offset=0.0, length=None, hook_secs=None):
    """Word-by-word highlighted captions (3-word chunks, pop-in) + a hook box from frame 0.

    words are in source time; offset shifts them to the trimmed clip's timeline.
    """
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,3,5,70,70,0,1
Style: Hook,DejaVu Sans,62,&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,16,0,5,110,110,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if hook:
        end = hook_secs if hook_secs else (length or 600)
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(end)},Hook,,0,0,0,,"
                     r"{\pos(%d,%d)\fad(0,250)}" % (W // 2, hook_y) + clean_word(hook))
    shifted = [(a - offset, b - offset, t) for a, b, t in words if b - offset > 0 and (length is None or a - offset < length)]
    chunks = chunk_words(shifted)
    for i, c in enumerate(chunks):
        c_end = c[-1][1] + 0.15
        if i + 1 < len(chunks):
            c_end = min(c_end, chunks[i + 1][0][0])
        texts = [clean_word(w[2]).upper() for w in c]
        # One event per spoken word: whole chunk visible, the current word in yellow.
        for j, w in enumerate(c):
            w_start = c[0][0] if j == 0 else w[0]
            w_end = c[j + 1][0] if j + 1 < len(c) else c_end
            if w_end <= w_start:
                continue
            body = " ".join(
                (r"{\c%s}%s{\c&H00FFFFFF&}" % (HIGHLIGHT, t)) if k == j else t for k, t in enumerate(texts)
            )
            pop = r"{\pos(%d,%d)" % (W // 2, cap_y) + (r"\fscx85\fscy85\t(0,90,\fscx100\fscy100)" if j == 0 else "") + "}"
            lines.append(f"Dialogue: 0,{_ass_time(w_start)},{_ass_time(w_end)},Cap,,0,0,0,,{pop}{body}")
    path.write_text(header + "\n".join(lines) + "\n")


def render(src, ass_name, out, layout, facecam, start, length, cwd, zoom=1.25):
    sw, sh, _ = probe(src)
    if layout == "stack" and facecam:
        x, y, fw, fh = facecam
        cam_h, game_h = 640, H - 640
        game_w_src = min(sw, int(sh * W / game_h) // 2 * 2)
        graph = (
            f"[0:v]split[a][b];"
            f"[a]crop={int(sw*fw)//2*2}:{int(sh*fh)//2*2}:{int(sw*x)}:{int(sh*y)},"
            f"scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h}[cam];"
            f"[b]crop={game_w_src}:{sh}:(iw-{game_w_src})/2:0,"
            f"scale={W}:{game_h}:force_original_aspect_ratio=increase,crop={W}:{game_h}[game];"
            f"[cam][game]vstack,setsar=1,ass={ass_name}[v]"
        )
    else:
        # Gameplay scaled up by `zoom` and centre-cropped to full width: a bigger subject
        # on a phone screen holds attention better than a thin letterboxed strip.
        fg_w = int(W * zoom) // 2 * 2
        graph = (
            f"[0:v]split[a][b];"
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=24:2,eq=brightness=-0.12[bg];"
            f"[b]scale={fg_w}:-2,crop={W}:ih[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,ass={ass_name}[v]"
        )
    run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{length:.2f}",
         "-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-r", "30",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         "-movflags", "+faststart", str(out)],
        cwd=cwd,
    )


# Text placement respects the platform UI: TikTok/Shorts cover roughly the top 200px
# and the bottom ~480px (caption, buttons), plus a strip on the right.
def text_positions(layout, facecam, src, zoom=1.25):
    """Returns (caption_y, hook_y, hook_secs). hook_secs=None means the hook stays up."""
    if layout == "stack" and facecam:
        return H - 560, 640, 3.5  # captions low on gameplay, hook on the seam for the first beats
    sw, sh, _ = probe(src)
    fg_h = int(int(W * zoom) // 2 * 2 * sh / sw)
    top_band = (H - fg_h) // 2
    hook_y = max(260, top_band // 2 + 60)
    cap_y = min(H - 500, (H + fg_h) // 2 + 150)
    return cap_y, hook_y, None


def verify_render(out, expect_len):
    """Check the rendered file before it goes to the queue. Returns problems (empty = ok)."""
    problems = []
    j = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
         "-of", "json", str(out)], check=True, capture_output=True, text=True).stdout)
    v = [s for s in j["streams"] if s["codec_type"] == "video"]
    a = [s for s in j["streams"] if s["codec_type"] == "audio"]
    dur = float(j["format"]["duration"])
    if not v or (v[0]["width"], v[0]["height"]) != (W, H):
        problems.append(f"video is not {W}x{H}")
    if abs(dur - expect_len) > 1.0:
        problems.append(f"duration {dur:.1f}s, expected {expect_len:.1f}s")
    if not a:
        problems.append("no audio track")
    else:
        vol = subprocess.run(["ffmpeg", "-v", "info", "-i", str(out), "-af", "volumedetect", "-vn", "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        m = re.search(r"mean_volume: (-?[\d.]+) dB", vol)
        if m and float(m.group(1)) < -45:
            problems.append(f"audio is near silent ({m.group(1)} dB)")
    blk = subprocess.run(["ffmpeg", "-v", "info", "-t", "1.5", "-i", str(out), "-vf", "blackdetect=d=1:pix_th=0.10",
                          "-an", "-f", "null", "-"], capture_output=True, text=True).stderr
    if "black_start" in blk:
        problems.append("opens on a black screen")
    return problems


def thumbnail(mp4, jpg):
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "0.3", "-i", str(mp4), "-frames:v", "1",
         "-vf", "scale=360:-2", "-q:v", "4", str(jpg)])


# ---------------------------------------------------------------- Claude
EDITOR_RULES = """You are the hook editor and compliance check for a gaming clip channel (TikTok first, then YouTube Shorts) that posts Twitch streamer clips for paid clipping campaigns. You only see a timestamped transcript plus metadata, not the video, so judge the moment from what is said and say so in risks when unsure.

COMPLIANCE (reject means decision="reject", one-line reason)
- Reject: gambling/casino streams; the transcript looks like song lyrics or a music video (copyrighted music); watch parties or reacting to TV, sports, movies or another creator's video; anything that only works with misleading or out-of-context framing; hateful/slur-centred moments; nothing happens (idle chat, setup, loading, AFK).
- If the streamer's campaign brief is given, follow it exactly; reject clips that break it.
- GTA 6 (game "Grand Theft Auto VI"): reject story cutscenes, mission outcomes, character deaths or twists, endings. Free-roam, chaos, physics, discoveries, RP and first reactions are fine. Hook, title and caption must not reveal story. If the moment involves graphic violence (kills, gore), the cut must open on the reaction or the chaos around it and land the violent beat at 8s or later (YouTube's first-7-seconds rule); add a risk "VIOLENCE: check first 7s". If licensed radio music may be audible, add a risk "MUSIC: mute/replace radio".

STRUCTURE (the beat pattern high-performing streamer Shorts share; build every cut to it)
1. 0-1s HOOK FRAME: open mid-action or on the loudest reaction, never on calm. The hook text supplies any missing context.
2. 1-3s STAKES: the viewer learns what could happen (chase, bet, first look, a dare) from the hook text or the first line.
3. ESCALATION: each few seconds raises the tension or the absurdity; cut any stretch where nothing changes.
4. PAYOFF: the single best second (crash, punchline, reveal, scream, fail) lands before 70% of the runtime when possible, so it isn't swiped away before it arrives.
5. REACTION TAIL: keep at most ~1.5s of the streamer's reaction after the payoff, then cut hard. Ending on a sharp reaction makes the loop feel seamless and drives rewatches.
Durations: 8-25s is the sweet spot for one-beat moments; up to 45s only for a story with several escalating beats. Shorter beats longer when in doubt.

FINDING THE CUT
- start_s: 1-3s before the payoff build starts, INSIDE the moment. Never open on setup, loading screens, menus, silent driving or a slow lead-in.
- end_s: at most ~1.5s after the payoff/reaction lands.
- If the clip needs more than 5s of context to make sense, or there is no clear payoff, score it 5 or lower.

HOOK (on-screen text box shown from frame 0, max 7 words)
- First frame, first spoken line and on-screen text must agree. Sound-off test: the text alone must stop a stranger's scroll in 2 seconds.
- Tease, never lie. If the clip can't pay off the hook, write a different hook.
- Specific beats generic: name the streamer, game, place (Vice City, Leonida), stakes, or exact words said. State stakes for non-players in 5 words or fewer.
- Banned: "wait for it", "you won't believe", "insane", "bro", "POV:" when it isn't one, emojis, all caps.
- Pick the best archetype for the moment: reaction-first, mid-action, verbatim quote, named callout, stakes-first, prediction, cold weirdness, identity ("Every GTA 5 player will feel this"), first-ever, then-vs-now (GTA 5 -> 6).

SCORE 1-10: would a stranger who doesn't know the streamer watch to the end and rewatch? 8+ = clear payoff, fast start, works sound-off. 5 or less = needs context, slow, or nothing happens.

TEXT
- title: YouTube title, <=60 chars, includes streamer and game, spoiler-free, no clickbait lies.
- caption: TikTok caption, <=120 chars: the hook idea + a soft prompt that invites comments (a question or a take) + "🎥 @<streamer login>". Do not put hashtags in caption.
- hashtags: 3 max, specific (game, streamer), plus any the brief requires."""

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "reason", "score", "payoff_s", "payoff", "start_s", "end_s",
                 "hook", "archetype", "title", "caption", "hashtags", "risks"],
    "properties": {
        "decision": {"type": "string", "enum": ["keep", "reject"]},
        "reason": {"type": "string", "description": "one line: why keep or reject"},
        "score": {"type": "integer"},
        "payoff_s": {"type": "number"},
        "payoff": {"type": "string", "description": "what happens at the payoff"},
        "start_s": {"type": "number"},
        "end_s": {"type": "number"},
        "hook": {"type": "string"},
        "archetype": {"type": "string"},
        "title": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}


def timed_transcript(words, max_words=10, max_gap=0.8):
    lines, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w[0] - cur[-1][1] > max_gap):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return "\n".join(f"[{c[0][0]:5.1f}s] " + " ".join(x[2] for x in c) for c in lines)


def review(clip, words, duration, cfg, streamer, max_rounds=2):
    """Claude picks the cut, hook and text; QC checks it and sends it back to fix.

    Loop: edit -> deterministic checks -> critic -> (issues? revise, up to max_rounds).
    Returns None when no API key is set (clip goes through unedited). Raises on API
    failure so the clip is marked "error" and retried, never queued unscored.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    login = streamer["login"]
    max_dur = cfg.get("max_duration", 45)
    meta = (
        f"Streamer: {clip['broadcaster_name']} (login: {login})\n"
        f"Source type: {streamer.get('source', 'permitted')}\nCampaign brief: {streamer.get('brief') or '(none given)'}\n"
        f"Game: {clip.get('_game') or 'unknown'}\n"
        f"Twitch clip title: {clip['title']}\nViews: {clip['view_count']}\n"
        f"Created: {clip['created_at']}\nDuration: {duration:.1f}s (max cut {max_dur}s)\n"
        f"Required hashtags: {' '.join(cfg.get('hashtags', [])) or '(none)'}\n\n"
        f"Transcript (seconds from clip start):\n{timed_transcript(words) or '(no speech detected)'}"
    )
    rv = claude(EDITOR_RULES, meta, REVIEW_SCHEMA)
    if rv is None:
        return {"decision": "reject", "reason": "model declined to review this clip", "score": 0, "qc": []}
    issues = []
    for rnd in range(max_rounds + 1):
        if rv.get("decision") == "reject":
            return {**rv, "qc": [], "qc_rounds": rnd}
        issues = qc_issues(rv, words, duration, login, max_dur)
        if not issues:
            issues = critique(rv, words)
        if not issues:
            log(f"QC passed after {rnd} revision(s)")
            return {**rv, "qc": [], "qc_rounds": rnd}
        log(f"QC round {rnd}: {issues}")
        if rnd == max_rounds:
            break
        fix = (meta + "\n\nYour previous edit:\n" + json.dumps(rv, ensure_ascii=False) +
               "\n\nQuality control found these problems. Return a corrected edit that fixes all of them "
               "(or reject the clip if it can't be fixed honestly):\n- " + "\n- ".join(issues))
        rv = claude(EDITOR_RULES, fix, REVIEW_SCHEMA) or rv
    # Still failing: keep it but cap the score so it can't look like a top pick.
    return {**rv, "qc": issues, "qc_rounds": max_rounds, "score": min(rv.get("score") or 0, 5)}


BANNED = ["wait for it", "you won't believe", "you wont believe", "insane", "bro", "pov:"]
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
WORD = re.compile(r"[A-Za-z0-9'’]+")


def qc_issues(rv, words, duration, login, max_dur):
    """Deterministic checks on Claude's edit. Returns a list of problems (empty = pass)."""
    if rv.get("decision") == "reject":
        return []
    out = []
    hook, title, cap = rv.get("hook", ""), rv.get("title", ""), rv.get("caption", "")
    if not hook.strip():
        out.append("hook is empty")
    if len(WORD.findall(hook)) > 7:
        out.append(f"hook has {len(WORD.findall(hook))} words; max 7")
    for b in BANNED:
        for field, text in (("hook", hook), ("title", title), ("caption", cap)):
            if re.search(r"(?<![a-z])" + re.escape(b) + r"(?![a-z])", text.lower()):
                out.append(f'{field} uses banned phrase "{b}"')
    if EMOJI.search(hook):
        out.append("hook contains emoji")
    if hook and hook.isupper() and len(hook) > 6:
        out.append("hook is all caps")
    if len(title) > 60:
        out.append(f"title is {len(title)} chars; max 60")
    if len(cap) > 120:
        out.append(f"caption is {len(cap)} chars; max 120")
    if f"@{login}".lower() not in cap.lower():
        out.append(f'caption must credit "🎥 @{login}"')
    if "#" in cap:
        out.append("caption contains hashtags; put them in hashtags only")
    if len(rv.get("hashtags", [])) > 3:
        out.append("more than 3 hashtags")
    try:
        start, end, pay = float(rv["start_s"]), float(rv["end_s"]), float(rv["payoff_s"])
    except (KeyError, TypeError, ValueError):
        return out + ["start_s/end_s/payoff_s missing or not numbers"]
    if not (0 <= start < end <= duration + 0.5):
        out.append(f"cut {start}-{end}s is outside the clip (0-{duration:.1f}s)")
    length = end - start
    if length < 5:
        out.append(f"cut is {length:.1f}s; minimum 5s")
    if length > max_dur:
        out.append(f"cut is {length:.1f}s; max {max_dur}s")
    if not (start <= pay <= end):
        out.append(f"payoff {pay}s is not inside the cut {start}-{end}s")
    else:
        if end - pay > 2.5:
            out.append(f"{end - pay:.1f}s after the payoff; end within ~1.5s of the reaction")
        if length >= 12 and (pay - start) / length > 0.85:
            out.append("payoff lands in the last 15% of the cut; start later")
    # Opening on dead air: the first speech should come quickly unless the clip has none.
    in_cut = [w for w in words if start <= w[0] < end]
    if words and in_cut and in_cut[0][0] - start > 2.5:
        out.append(f"first speech is {in_cut[0][0] - start:.1f}s into the cut; start closer to the action")
    return out


CRITIC_RULES = """You are the quality-control reviewer for a gaming Shorts channel. You get a proposed edit (the cut's transcript, the on-screen hook, title and caption). Fail it if ANY of these hold:
- The hook promises something the cut's transcript doesn't deliver (overpromise / misleading framing).
- With sound off and zero context, the hook alone wouldn't make a stranger stop in 2 seconds (generic, vague, or needs knowledge of the streamer).
- The hook, title or caption reveals GTA 6 story (missions, deaths, twists, endings).
- The cut opens on setup instead of inside the moment, or the payoff is missing from the cut.
- The streamer isn't credited, or text contains slurs.
Be strict but concrete: each issue must say what to change."""

CRITIC_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["pass", "issues"],
    "properties": {"pass": {"type": "boolean"}, "issues": {"type": "array", "items": {"type": "string"}}},
}


def claude(system, content, schema, effort="medium"):
    """One structured-output call with retries. Raises when every attempt fails."""
    key = os.environ["ANTHROPIC_API_KEY"]
    body = {
        "model": os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5"),
        # Haiku 5.5 thinks by default and thinking counts toward max_tokens.
        "max_tokens": 8000,
        "system": system,
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        "messages": [{"role": "user", "content": content}],
    }
    last = None
    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json=body, timeout=120,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            j = r.json()
            if j.get("stop_reason") == "refusal":
                return None
            if j.get("stop_reason") == "max_tokens":
                raise RuntimeError("hit max_tokens before answering")
            return json.loads("".join(b.get("text", "") for b in j["content"] if b.get("type") == "text"))
        except (RuntimeError, ValueError, requests.RequestException) as e:
            last = e
            log(f"claude attempt {attempt + 1} failed: {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"claude call failed: {last}")


def critique(rv, words):
    start, end = float(rv["start_s"]), float(rv["end_s"])
    cut = [(a - start, b - start, t) for a, b, t in words if start <= a < end]
    content = (
        f"Hook (on screen from 0s): {rv['hook']}\nTitle: {rv['title']}\nCaption: {rv['caption']}\n"
        f"Payoff claimed at {float(rv['payoff_s']) - start:.1f}s into the cut: {rv.get('payoff', '')}\n"
        f"Cut length: {end - start:.1f}s\nCut transcript (seconds from cut start):\n"
        f"{timed_transcript(cut) or '(no speech in the cut)'}"
    )
    res = claude(CRITIC_RULES, content, CRITIC_SCHEMA, effort="low")
    if res is None:
        return ["critic declined to review"]
    return [] if res.get("pass") else (res.get("issues") or ["critic failed it without detail"])


def pick_cut(rv, duration, max_dur):
    """Sanity-check Claude's cut; fall back to the whole clip if it's nonsense."""
    try:
        start = max(0.0, float(rv["start_s"]) - 0.2)
        end = min(duration, float(rv["end_s"]) + 0.4)
    except (KeyError, TypeError, ValueError):
        return 0.0, min(duration, max_dur)
    if end - start < 5:
        start, end = max(0.0, end - 5), max(end, min(duration, start + 5))
    if end <= start:
        return 0.0, min(duration, max_dur)
    return start, min(end, start + max_dur)


# ---------------------------------------------------------------- storage
def gh(*args, check=True):
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def ensure_release():
    if gh("release", "view", RELEASE_TAG, check=False).returncode != 0:
        gh("release", "create", RELEASE_TAG, "--title", "Clip store", "--latest=false",
           "--notes", "Rendered clips, managed by the Clipline worker. Don't edit by hand.")


def upload(*files):
    gh("release", "upload", RELEASE_TAG, *[str(f) for f in files], "--clobber")


def delete_asset(name):
    gh("release", "delete-asset", RELEASE_TAG, name, "-y", check=False)


def cleanup(queue, decisions, keep_days):
    t = now()
    for item in queue:
        d = decisions.get(item["id"])
        if not item.get("stored") or not d:
            continue
        age = t - dt.datetime.fromisoformat(d["at"].replace("Z", "+00:00"))
        if d["action"] == "skipped" or age > dt.timedelta(days=keep_days):
            delete_asset(item["video"])
            delete_asset(item["thumb"])
            item["stored"] = False
            log("deleted files for", item["id"], d["action"])
    cutoff = t - dt.timedelta(days=30)
    return [q for q in queue if q.get("stored") or dt.datetime.fromisoformat(q["added_at"].replace("Z", "+00:00")) > cutoff]


# ---------------------------------------------------------------- main
def main():
    if not (os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET")):
        log("Twitch keys not set yet (TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET) — skipping. DONE")
        return 0
    cfg = yaml.safe_load((ROOT / "config.yml").read_text())
    queue = load_json(QUEUE, [])
    decisions = load_json(DECISIONS, {})
    # Clips that errored get retried on later runs, up to 3 attempts.
    retry = {q["id"]: q.get("attempts", 1) for q in queue if q.get("status") == "error" and q.get("attempts", 1) < 3}
    queue = [q for q in queue if q["id"] not in retry]
    seen = {q["id"] for q in queue}
    streamers = {s["login"].lower(): s for s in cfg["streamers"]}

    ensure_release()
    queue = cleanup(queue, decisions, cfg.get("keep_days", 7))
    save_json(QUEUE, queue)

    tw = Twitch()
    ids = tw.user_ids(list(streamers))
    for login in streamers:
        if login not in ids:
            log(f"WARNING: Twitch user '{login}' not found")
    games_filter = {g.lower() for g in cfg.get("games") or []}

    # Clip links pasted into "Run workflow" (CLIPLINE_CLIPS) are processed even if seen
    # before, which also re-renders a clip with the current settings.
    manual_ids = re.findall(r"(?:clip/|clips\.twitch\.tv/)?([A-Za-z0-9_-]{20,})", os.environ.get("CLIPLINE_CLIPS", ""))
    candidates = []
    for c in tw.clips_by_id(manual_ids):
        c["_login"] = c["broadcaster_name"].lower()
        c["_manual"] = True
        candidates.append(c)
    for login, bid in ids.items():
        for c in tw.top_clips(bid, cfg.get("lookback_hours", 48)):
            if c["id"] not in seen and c["view_count"] >= cfg.get("min_views", 0):
                c["_login"] = login
                candidates.append(c)
    names = tw.game_names([c.get("game_id") for c in candidates])
    for c in candidates:
        c["_game"] = names.get(c.get("game_id"), "")
    if games_filter:
        candidates = [c for c in candidates if c.get("_manual") or c["_game"].lower() in games_filter]
    # Manual clips first, then highest views per minute since clipped (fresh + hot beats old + big).
    def heat(c):
        age_min = max(10.0, (now() - dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00"))).total_seconds() / 60)
        return (not c.get("_manual"), -c["view_count"] / age_min)
    candidates.sort(key=heat)
    log(f"{len(candidates)} new candidate clips")

    added = 0
    for c in candidates:
        if added >= cfg.get("max_new_per_run", 6) and not c.get("_manual"):
            break
        s = streamers.get(c["_login"], {"login": c["_login"], "layout": "blur"})
        wd = WORK / c["id"]
        shutil.rmtree(wd, ignore_errors=True)
        wd.mkdir(parents=True)
        item = {"id": c["id"], "streamer": c["broadcaster_name"], "login": c["_login"], "game": c["_game"],
                "twitch_title": c["title"], "views": c["view_count"], "created_at": c["created_at"],
                "duration": c["duration"], "source_url": c["url"], "added_at": iso(now()), "stored": False}
        try:
            src = wd / "src.mp4"
            run(["yt-dlp", "-q", "--no-warnings", "-f", "best[ext=mp4]/best", "-o", str(src), c["url"]])
            words = transcribe(src)
            _, _, src_dur = probe(src)
            rv = review(c, words, src_dur, cfg, s)
            transcript = " ".join(w[2] for w in words)
            item["transcript"] = transcript[:500]
            max_dur = cfg.get("max_duration", 60)
            if rv is None:  # no API key: whole clip, Twitch title
                rv = {"decision": "keep", "score": None, "hook": "", "title": c["title"],
                      "hashtags": cfg.get("hashtags", []), "caption": "", "risks": []}
                start, end = 0.0, min(src_dur, max_dur)
            else:
                start, end = pick_cut(rv, src_dur, max_dur)
            tags = list(dict.fromkeys(cfg.get("hashtags", []) + [h if h.startswith("#") else "#" + h for h in rv.get("hashtags", [])]))
            item.update(score=rv.get("score"), reason=rv.get("reason", ""), hook=rv.get("hook", ""),
                        archetype=rv.get("archetype", ""), payoff=rv.get("payoff", ""),
                        title=rv.get("title") or c["title"], caption=rv.get("caption", ""),
                        hashtags=tags[:max(3, len(cfg.get("hashtags", [])))], risks=rv.get("risks", []),
                        cut=[round(start, 1), round(end, 1)], qc_rounds=rv.get("qc_rounds"))
            if rv.get("qc"):
                item["risks"] = item["risks"] + ["QC: " + q for q in rv["qc"]]
            if rv.get("decision") == "reject" and not c.get("_manual"):
                item["status"] = "filtered"
                log(f"rejected {c['id']}: {rv.get('reason')}")
            elif rv.get("score") is not None and rv["score"] < cfg.get("min_score", 0) and not c.get("_manual"):
                item["status"] = "filtered"
                log(f"filtered {c['id']} score={rv['score']}: {rv.get('reason')}")
            else:
                if rv.get("decision") == "reject":  # manual clips are rendered anyway, flagged
                    item["risks"] = [f"REJECTED: {rv.get('reason')}"] + item["risks"]
                layout, cam = s.get("layout", "blur"), s.get("facecam")
                zoom = float(s.get("zoom", cfg.get("zoom", 1.25)))
                cap_y, hook_y, hook_secs = text_positions(layout, cam, src, zoom)
                build_ass(words, wd / "subs.ass", cap_y, hook=item["hook"], hook_y=hook_y,
                          offset=start, length=end - start, hook_secs=hook_secs)
                safe = re.sub(r"[^A-Za-z0-9_-]", "", c["id"])[:80]
                out, jpg = wd / f"{safe}.mp4", wd / f"{safe}.jpg"
                render(src, "subs.ass", out, layout, cam, start, end - start, wd, zoom)
                bad = verify_render(out, end - start)
                if bad:
                    raise RuntimeError("render check failed: " + "; ".join(bad))
                thumbnail(out, jpg)
                upload(out, jpg)
                item.update(status="pending", video=out.name, thumb=jpg.name, stored=True, duration=round(end - start, 1))
                added += 1
                log(f"queued {c['id']} score={item['score']} cut={item['cut']} hook={item['hook']!r}")
        except Exception as e:  # noqa: BLE001
            item["status"] = "error"
            item["error"] = str(e)[:300]
            item["attempts"] = retry.get(c["id"], 0) + 1
            log(f"ERROR on {c['id']}: {e}")
        queue = [q for q in queue if q["id"] != item["id"]] + [item]
        save_json(QUEUE, queue)  # save as we go so a timeout keeps progress
        shutil.rmtree(wd, ignore_errors=True)

    log(f"DONE — {added} new clips in your queue")


if __name__ == "__main__":
    sys.exit(main())
