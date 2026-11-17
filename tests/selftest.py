"""Offline self-test for the worker: QC rules, captions, render + render checks, QC loop.

No network or API keys. Runs in CI before the worker so a broken change can't
reach the queue. Usage: python tests/selftest.py
"""
import json
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "worker"))
import clipline as c  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


words = [(10 + i * 0.4, 10 + i * 0.4 + 0.35, w) for i, w in enumerate(
    "oh my god he just flew into the ocean what the fuck nigga that was crazy chat did you see that".split())]
good = {"decision": "keep", "reason": "x", "score": 8, "payoff_s": 13.0, "payoff": "car flies", "start_s": 9.5,
        "end_s": 14.5, "hook": "Kai's first drive into Vice City", "archetype": "first-ever",
        "title": "Kai Cenat's first Vice City drive | GTA 6",
        "caption": "His first 10 seconds in Vice City. Worse driver than you? 🎥 @kaicenat",
        "hashtags": ["#GTA6", "#KaiCenat"], "risks": []}

# --- deterministic QC
check("qc passes a good edit", c.qc_issues(good, words, 30, "kaicenat", 45) == [], c.qc_issues(good, words, 30, "kaicenat", 45))
cases = {
    "long hook": {"hook": "this is a hook that has way too many words in it"},
    "banned phrase": {"hook": "Wait for it, Kai crashes"},
    "insane": {"title": "Insane GTA 6 crash"},
    "no credit": {"caption": "Who drives worse?"},
    "hashtag in caption": {"caption": "Crash #gta6 🎥 @kaicenat"},
    "payoff outside cut": {"payoff_s": 20.0},
    "long tail": {"end_s": 18.0},
    "too short": {"start_s": 12.0, "end_s": 14.0},
    "outside clip": {"end_s": 40.0},
    "all caps hook": {"hook": "KAI CRASHES IN VICE CITY"},
    "slow open": {"start_s": 6.0, "end_s": 14.0},
}
for name, patch in cases.items():
    issues = c.qc_issues({**good, **patch}, words, 30, "kaicenat", 45)
    check(f"qc catches {name}", bool(issues), "no issue raised")
check("qc skips rejects", c.qc_issues({"decision": "reject"}, words, 30, "kaicenat", 45) == [])
check("brother is not 'bro'", c.qc_issues({**good, "hook": "Kai's brother crashes in Vice City"}, words, 30, "kaicenat", 45) == [])

# --- caption masking
for raw, bad in [("nigga", "nigg"), ("niggas", "nigg"), ("fuck", "fuck"), ("motherfucker", "fuck"), ("shit", "shit")]:
    check(f"mask {raw[:2]}…", bad not in c.clean_word(raw).lower(), c.clean_word(raw))

# --- QC loop with a fake Claude: first edit fails, revision passes
calls = []


def fake_claude(system, content, schema, effort="medium"):
    calls.append(system[:20])
    if schema is c.CRITIC_SCHEMA:
        return {"pass": True, "issues": []}
    return dict(good, hook="Wait for it") if len([x for x in calls if x == c.EDITOR_RULES[:20]]) == 1 else dict(good)


real, c.claude = c.claude, fake_claude
import os  # noqa: E402
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
clip = {"broadcaster_name": "KaiCenat", "title": "t", "view_count": 1, "created_at": "2026-11-17T00:00:00Z", "_game": "Grand Theft Auto VI"}
rv = c.review(clip, words, 30, {"max_duration": 45}, {"login": "kaicenat"})
check("loop revises a failing edit", rv["hook"] == good["hook"] and rv["qc"] == [] and rv["qc_rounds"] == 1, rv)
calls.clear()
c.claude = lambda s, ct, sc, effort="medium": {"pass": False, "issues": ["hook overpromises"]} if sc is c.CRITIC_SCHEMA else dict(good)
rv = c.review(clip, words, 30, {"max_duration": 45}, {"login": "kaicenat"})
check("unfixable edit is flagged and score capped", rv["qc"] and rv["score"] <= 5, rv)
c.claude = real

# --- render + verification on a synthetic 1080p clip
with tempfile.TemporaryDirectory() as td:
    wd = pathlib.Path(td)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=30", "-shortest", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", str(wd / "src.mp4")], check=True)
    for layout, cam in [("blur", None), ("stack", [0, 0, 0.25, 0.3])]:
        cy, hy, hs = c.text_positions(layout, cam, wd / "src.mp4")
        check(f"{layout}: text clear of platform UI", 200 <= hy and cy <= c.H - 480, (cy, hy))
        c.build_ass(words, wd / "subs.ass", cy, hook=good["hook"], hook_y=hy, offset=9.5, length=5.0, hook_secs=hs)
        ass = (wd / "subs.ass").read_text()
        check(f"{layout}: captions masked", "nigga" not in ass.lower() and "fuck" not in ass.lower())
        out = wd / f"{layout}.mp4"
        c.render(wd / "src.mp4", "subs.ass", out, layout, cam, 9.5, 5.0, wd)
        probs = c.verify_render(out, 5.0)
        check(f"{layout}: render passes verification", probs == [], probs)
    # verification must catch a silent black clip
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:size=1080x1920:rate=30:duration=4",
                    "-f", "lavfi", "-i", "anullsrc", "-t", "4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(wd / "bad.mp4")], check=True)
    probs = c.verify_render(wd / "bad.mp4", 8.0)
    check("verification catches black/silent/wrong length", len(probs) >= 3, probs)

print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
sys.exit(1 if failures else 0)
