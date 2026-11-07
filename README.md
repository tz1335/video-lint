# video-lint

**Deterministic QA for finished short videos.** Point it at an mp4 and it checks the first frame and first two seconds, black and frozen stretches, text under TikTok / Douyin / Reels / Shorts UI zones, cut-on-beat timing, loudness, and whether sound effects are buried under the music. It writes a human report, a JSON report for agents, and contact sheets. No model calls: same input, same answer.

**程序化短视频的成片质检。** 给它一个 mp4，它会查第 0 帧和前 2 秒、黑场和静止、字幕有没有被抖音 / TikTok / Reels / Shorts 的界面挡住、硬切卡不卡拍、响度、音效有没有被配乐盖住，输出给人看的报告、给 agent 读的 JSON 和联系表。不调用任何模型，同样的输入永远得到同样的结论。

It started inside a Remotion project that makes short explainer videos; every rule comes from a real published video and its retention data (the reasons are written next to each rule in `lint.py`).

## Quick start

```bash
pip install -r requirements.txt     # numpy, pillow, scipy; librosa is optional (beat estimation)
python -I lint.py final.mp4
python -I lint.py final.mp4 --sidecar examples/sidecar.example.json --platform tiktok --strict
```

Requires `ffmpeg` / `ffprobe` on PATH. See `SKILL.md` for the full option list, the sidecar format, and how to read `report.json`. `SKILL.md` follows the Agent Skills format, so this folder can be dropped into any agent that supports skills.

## What it checks

| Check | Rule |
|---|---|
| Frame 0 | near-black ✗; under 0.5% bright pixels ✗; clearly darker than 0.5–2 s (fade-in) ⚠ |
| First 2 seconds | black frames ⚠; motion relative to the rest of the video (recorded) |
| Black / frozen | black runs mid-video ⚠; frozen ≥ 1 s in the first 3 s ⚠ |
| Hits (with sidecar) | darken before, freeze N frames, burst after |
| Cut on beat | hard cuts more than ±1.5 frames off the grid ⚠ / ✗ |
| Loudness | true peak > −1 dBFS ⚠, > 0 ✗; integrated LUFS only when `--lufs` is given |
| Sound effects | full-band or 1.2–5 kHz rise over the moment before; or, with a stem, effect-to-music ratio |
| Text (with text layer) | glyphs under a platform UI zone ✗ / ⚠; text touching the frame edge ⚠; text size recorded |
| Opening rules | frame 0 has text big enough to read; a hook in the top half (portrait); a number within 8 s |

## Status and exit codes

`pass` (0) · `fail` (2) · `unmeasured` (3: something could not be checked — not a pass). Exit code 1 is reserved for crashes.

## License

MIT (see `LICENSE`). Platform UI zone data from [reelsmith/safezone](https://github.com/reelsmith/safezone), MIT — see `THIRD_PARTY.md`.
