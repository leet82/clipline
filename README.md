# Clipline

Twitch top clips → vertical 9:16 Shorts with burned-in captions → review queue on your phone → share sheet to TikTok / Shorts / Reels.

Everything runs on GitHub: Actions does the work every 4 hours and publishes the review app to GitHub Pages. $0/month.

```
GitHub Actions (every 4h)                        GitHub Pages (phone app)
  Twitch Get Clips (top by views)                  Post ↗ = iOS share sheet (caption copied)
  yt-dlp download → faster-whisper captions        Skip / Undo / Run now
  Claude: cut, hook, compliance, caption          writes data/decisions.json via GitHub API
  ffmpeg render → clip-store release  ──deploy──▶  (token saved on your phone only)
```

## Setup
1. **Repo secrets** (Settings → Secrets and variables → Actions): `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, optional `ANTHROPIC_API_KEY`.
2. **Phone token:** github.com/settings/personal-access-tokens/new → repo `clipline` only → Contents: Read and write, Actions: Read and write.
3. **Streamer:** edit `config.yml` → the Whop campaign streamer's Twitch login.
4. Open `https://thomasleegh.github.io/clipline/` → Share → Add to Home Screen → paste the token → **Run now**.

## Pipeline and where the time goes
One GitHub Actions job runs these steps in order (measured on the 4-core runner):

| Stage | Time | Notes |
|---|---|---|
| Runner setup + `apt install ffmpeg` + pip | ~45-60s | fixed cost per run |
| Self-test | ~11s | offline; stops the run if anything is broken |
| Per clip: download (yt-dlp) | 2-5s | |
| Per clip: transcribe (Whisper base, CPU) | ~5s | |
| Per clip: Claude edit + QC | 5-30s | 2 calls when it passes first time, up to 6 with fixes |
| Per clip: render (ffmpeg, 1080x1920) | ~0.7s per second of clip | was ~1.5s; 30fps first + low-res blur |
| Per clip: verify + upload | 2-4s | |
| Save queue, build app, deploy Pages | ~15s | |

Rejected clips stop after the Claude step, so they cost about 10-40s each. Each clip's stage timings are saved in `data/queue.json` (`timings`) and in the worker log.

## App side panel (☰)
- **Runs:** live status of the last 6 runs, **Stop** on anything queued or running (clips finished so far are kept).
- **Start a run:** Normal (config.yml), More (15 clips, 3 days back) or Max (30 clips, 7 days back), optionally with Twitch clip links.
- **Schedule:** pause or resume the 4-hourly auto-runs (writes `data/control.json`; manual runs still work).
- **Archive:** every clip from the last 30 days (posted, to review, skipped, filtered, errors) with the reason and links.

## Daily loop
Open app → watch → edit caption → **Post ↗** → TikTok / YouTube → paste caption → submit the post link on Whop.

## How clips are edited
Claude (Haiku 5.5) reads each clip's timestamped transcript and returns a cut, a hook and the text:
- **Structure:** hook frame (0-1s, mid-action or peak reaction), stakes (1-3s), escalation, payoff before ~70% of the runtime, then at most ~1.5s of reaction so the clip loops. Most cuts are 8-25s.
- **Hook box** (≤7 words) on screen from frame 0, clear of the TikTok/Shorts UI zones. Captions highlight each word as it's spoken; slurs are masked and swears softened.
- **Compliance:** rejects gambling, music, watch parties, misleading framing and GTA 6 story spoilers; flags violence in the first 7s and radio music as risks in the app.
- **Quality control loop:** every edit goes through hard checks (hook ≤7 words, banned phrases, payoff inside the cut and before the end, ≤2.5s tail, no dead-air open, title/caption length, streamer credit, ≤3 hashtags), then a stricter Claude critic (overpromising, sound-off test, spoilers). Failures go back to Claude to fix, up to 2 rounds; anything still failing is capped at 5/10 and its issues show as risks.
- **Render verification:** every mp4 is checked for 1080x1920, the expected length, audible audio and no black opening before upload. A failed check marks the clip as an error so it's retried.
- **Self-test:** `tests/selftest.py` runs before every worker run (offline, no API calls) and stops the run if any QC rule, caption mask or render check breaks.
- The app shows the hook, payoff, risks and a TikTok caption, sorted best score first.
- **Clip a specific moment:** Actions → Clipline worker → Run workflow → paste Twitch clip links. It also re-renders old clips.

## Notes
- Each run takes about 5–15 min. Change the schedule in `.github/workflows/worker.yml`.
- Skipped clip files are deleted on the next run; posted ones after `keep_days`.
- Put any hashtags a Whop campaign requires in `config.yml`.
