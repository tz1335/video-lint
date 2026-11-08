---
name: video-lint
description: Deterministic QA for a finished short video (mp4) before it is published. Use after rendering or exporting any short-form video — Remotion, ffmpeg, After Effects, an AI video tool — to check the first frame and first two seconds, black or frozen stretches, text under TikTok / Douyin / Reels / Shorts UI zones, cut-on-beat timing, loudness and true peak, whether sound effects are buried under the music, and to get a contact sheet and a JSON report an agent can act on. No model calls; every rule is a fixed threshold.
---

# video-lint

Checks a rendered video with fixed rules and writes a report you can act on. It never calls a model, so the result does not change between runs or agents.

## Run it

```bash
pipx install "video-lint[ocr] @ git+https://github.com/tz1335/video-lint@v0.5.0"
video-lint final.mp4 --out lint-report
video-lint final.mp4 --sidecar sidecar.json --platform tiktok --lufs -14 --strict
# 不安装：pip install -r requirements.txt && python -I lint.py final.mp4
```

Needs `ffmpeg` and `ffprobe` on PATH.

Outputs in `--out` (default `<video>-lint/`): `report.md` (for people), `report.json` (for agents), `sheet-*.jpg` contact sheets with the platform UI zones drawn on portrait videos, `frame0.jpg`, `hits.jpg`, `curve.png` (brightness and motion over time).

## Read the result

`report.json` has `status`:

| status | meaning | `--strict` exit code |
|---|---|---|
| `pass` | nothing failed, everything that should be checked was checked | 0 |
| `fail` | at least one ✗ | 2 |
| `unmeasured` | no ✗, but something could not be checked (no audio track, a hit outside the video, cuts without a beat grid) — not a pass | 3 |

Exit code 1 means the script itself crashed. Each entry in `checks` is `{id, level: fail|warn|skip|pass|info, title, detail, data}`. Fix the `fail` items first; `warn` items need a judgement; `info` is context.

When fixing, change the source (composition, timeline, mix) and re-render; do not patch the mp4.

## Give it more context (optional)

A sidecar JSON unlocks the timing checks. Times are in **frames**, the beat grid in **seconds**:

```json
{
  "format": "video-lint-sidecar/0",
  "fps": 30,
  "durationFrames": 900,
  "root": "/abs/path/to/project",
  "sources": {"composition": {"file": "src/Video.tsx", "line": 12}},
  "shots": [{"name": "hook", "from": 0, "to": 90, "source": {"file": "src/scenes/Hook.tsx", "line": 8}, "defined": {"file": "src/timeline.ts", "line": 4}}],
  "hits": [{"name": "drop", "frame": 243, "darkFrom": 220, "freeze": 3}],
  "sfx": [{"name": "impact", "frame": 243, "volume": 0.8}],
  "beats": {"period": 0.6663, "offset": 0.104, "source": "measured", "times": [0.104, 0.770]},
  "captions": [{"from": 0, "to": 60, "text": "First line"}],
  "platform": "douyin"
}
```

- `shots` → one contact-sheet frame per shot (4 frames before it cuts away)
- `source` / `defined` / `sources.composition` (optional, paths relative to `root`) → every fail / warn gets `where: [{file, line, shot, from, to, timing, path}]` in `report.json`, the MCP result, `report.md` and the console. Open `path` at `line` to fix the shot; `timing` is where its frames are set
- `hits` → darken / freeze / burst checks around the frame
- `beats` → cut-on-beat check (±1.5 frames); without it the grid is estimated from audio and can only warn
- `sfx` + `--loud name1,name2` → is each sound effect louder than the moment before it
- `--sfx-audio sfx-only.wav` → measures each effect against the music from a stem instead (much more reliable)
- `--text-dir` → PNGs of the same frames with all text painted pure magenta `#ff00ff`; the most precise source for "text inside UI zones", "text off frame" and text size
- without `--text-dir`, if `rapidocr-onnxruntime` is installed, text is found with OCR on the same frames (boxes shrunk to glyph height; may miss tiny or stylised text). `--no-ocr` turns this off; with neither, the zones are only drawn for a human to look at

## Thresholds worth knowing

- `--captions file.srt|.vtt|.ass` → caption reading speed (Netflix: ≤ 9 CJK chars/s, ≤ 20 Latin chars/s, ≥ 5/6 s on screen) and the opening-line rules, without a sidecar
- Upload spec for `--platform` (codec, pixel format, fps, resolution, aspect, duration limit, audio) warns only; values live in `video_lint/specs.json`
- `.video-lint.toml` (next to the video or above) sets defaults — platform, lufs, loud, captions, ignore = [check ids, globs ok], [levels] overrides; flags win
- `report.html` is written every run: one self-contained file to hand to a person
- MCP: `video-lint-mcp` exposes `lint_video` and `list_platforms` (install the `[mcp]` extra)
- `--compare <previous report.json or folder>` → a `compare` check: fixed / new / still there; new problems warn
- Mono fold-down (phone speakers): whole video drops > 4.5 dB, or any 0.5 s window > 9 dB, when L+R are summed → warn (normal stereo ≈ −3 dB)
- Flashing (WCAG 2.3.1): more than 3 flashes, or 3 red flashes, in any one second over ~11% of the frame → fail; `--no-flash` skips it
- Frame 0: near-black → fail; under 0.5% of pixels brighter than 60/255 → fail
- Text size on frame 0: line height ÷ video **width** (both orientations fill the phone width) — under 2.5% fail, under 4% warn
- UI zones: TikTok / Reels / Shorts numbers from reelsmith/safezone (MIT, see THIRD_PARTY.md); Douyin reuses TikTok
- Loudness is only judged when you pass `--lufs`; true peak above −1 dBFS always warns
