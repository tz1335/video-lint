# video-lint

**Deterministic QA for finished short videos.** Point it at an mp4 and it checks the first frame and first two seconds, black and frozen stretches, text under TikTok / Reels / Shorts UI zones, cut-on-beat timing, loudness, and whether sound effects are buried under the music. It writes a human report, a JSON report for agents, and contact sheets. No model calls: same input, same answer.

**短视频成片质检，规则全是确定性的。** 给它一个 mp4，它会查第 0 帧和前 2 秒、黑场和静止画面、字幕有没有被抖音（TikTok）/ Reels / Shorts 的界面挡住、硬切卡不卡拍、响度，以及音效有没有被配乐盖住。输出给人看的报告、给 agent 读的 JSON 报告和联系表。不调用任何模型：同样的输入，永远得到同样的结论。

It started inside a Remotion project that makes short explainer videos; every rule comes from a real published video and its retention data (the reasons are written next to each rule in `lint.py`).

它最早是在一个用 Remotion 做知识短视频的工程里写出来的。每一条规则都来自一支真实发布过的视频和它的留存数据，`lint.py` 里每条规则旁边都写了来由。

## Quick start · 快速开始

```bash
pip install -r requirements.txt     # numpy, pillow, scipy; librosa is optional (beat estimation)
python -I lint.py final.mp4
python -I lint.py final.mp4 --sidecar examples/sidecar.example.json --platform tiktok --strict
```

Requires `ffmpeg` / `ffprobe` on PATH. See `SKILL.md` for the full option list, the sidecar format, and how to read `report.json`. `SKILL.md` follows the Agent Skills format, so this folder can be dropped into any agent that supports skills.

需要 `ffmpeg` / `ffprobe` 在 PATH 里。完整参数、旁注（sidecar）格式、`report.json` 怎么读，见 `SKILL.md`。`SKILL.md` 按 Agent Skills 格式写，所以整个目录可以直接放进任何支持 skill 的 agent 里用。`librosa` 是可选的，只在没有旁注拍子网格、要从音频估拍子时用到。

## What it checks · 查什么

| Check · 检查 | Rule · 规则 |
|---|---|
| Frame 0 · 第 0 帧 | near-black ✗; under 0.5% bright pixels ✗; clearly darker than 0.5–2 s (fade-in) ⚠<br>近乎全黑 ✗；亮像素不到 0.5% ✗；明显比 0.5–2 秒暗（淡入开场）⚠ |
| First 2 seconds · 前 2 秒 | black frames ⚠; motion relative to the rest of the video (recorded)<br>有黑帧 ⚠；运动量和全片相比的倍数（只记录） |
| Black / frozen · 黑场和静止 | black runs mid-video ⚠; frozen ≥ 1 s in the first 3 s ⚠<br>片中黑场 ⚠；前 3 秒有 1 秒以上不动 ⚠ |
| Hits (with sidecar) · 爆点（需要旁注） | darken before, freeze N frames, burst after<br>爆点前收暗、最后 N 帧静止、爆点后亮起 |
| Cut on beat · 硬切卡拍 | hard cuts more than ±1.5 frames off the grid ⚠ / ✗<br>硬切离拍点超过 ±1.5 帧 ⚠ / ✗ |
| Loudness · 响度 | true peak > −1 dBFS ⚠, > 0 ✗; integrated LUFS only when `--lufs` is given<br>真峰值高于 −1 dBFS ⚠、高于 0 ✗；给了 `--lufs` 才判整片响度 |
| Sound effects · 音效 | full-band or 1.2–5 kHz rise over the moment before; or, with a stem, effect-to-music ratio<br>比前一刻高出多少（全频或 1.2–5 kHz）；有音效分轨时改用音效和配乐的能量比 |
| Text (with text layer) · 文字（需要文字层） | glyphs under a platform UI zone ✗ / ⚠; text touching the frame edge ⚠; text size recorded<br>字被平台界面挡住 ✗ / ⚠；文字贴边 ⚠；字高只记录 |
| Opening rules · 开头规则 | frame 0 has text big enough to read; a hook in the top half (portrait); a number within 8 s<br>第 0 帧有够大的字；竖屏上半屏有钩子；8 秒内出现数字 |

## Platforms · 平台

UI zone data ships for three platforms (`--platform`); the numbers come from [reelsmith/safezone](https://github.com/reelsmith/safezone) and are approximations averaged over recent iOS and Android phones.

自带三个平台的界面遮挡区数据（`--platform`），数字取自 [reelsmith/safezone](https://github.com/reelsmith/safezone)，是近几年 iOS 和安卓手机的平均近似值。

| `--platform` | Platform · 平台 | Status · 状态 |
|---|---|---|
| `tiktok` / `douyin` | TikTok / 抖音 | supported; Douyin reuses the TikTok numbers (same layout)<br>已支持；抖音界面和 TikTok 布局相同，直接沿用 TikTok 的数字 |
| `reels` | Instagram Reels | supported · 已支持 |
| `shorts` | YouTube Shorts | supported · 已支持 |
| `none` | — | no zones (landscape, or any other player)<br>不画遮挡区（横屏视频，或其它播放器） |

Other platforms where the same checks are useful. Everything except the UI zones already works on them today; adding a platform only needs its zone rectangles in `zones.json` (measure a screenshot of the feed on a phone).

下面这些平台同样用得上。除了界面遮挡区，其它检查现在就能直接用；要加一个平台，只需要在 `zones.json` 里补上它的遮挡区矩形（拿手机截一张信息流的图量出来）。

| Platform · 平台 | Feed format · 主要画幅 | Notes · 说明 |
|---|---|---|
| Kuaishou · 快手 | 9:16 | layout broadly like Douyin; zones not measured yet<br>布局大体和抖音相近，遮挡区还没量 |
| WeChat Channels · 微信视频号 | 9:16, also landscape | zones not measured yet<br>遮挡区还没量 |
| Xiaohongshu · 小红书 | 3:4 and 9:16 | 3:4 is the native card ratio; video notes also play 9:16<br>3:4 是笔记封面原生比例，视频笔记也播 9:16 |
| Bilibili · B 站 | 16:9 (main), 9:16 (Story mode) | landscape has few overlays; the vertical Story feed has a right rail<br>横屏基本没有遮挡；竖屏 Story 模式有右侧按钮列 |
| Facebook Reels | 9:16 | layout broadly like Instagram Reels; zones not measured yet<br>布局大体和 Instagram Reels 相近，遮挡区还没量 |
| Snapchat Spotlight | 9:16 | zones not measured yet<br>遮挡区还没量 |
| Pinterest (video pins) | 2:3, 9:16 | zones not measured yet<br>遮挡区还没量 |
| LinkedIn / X (Twitter) video | 1:1, 4:5, 16:9 | in-feed autoplay starts muted, so the first frame and burned-in captions matter even more<br>信息流里默认静音自动播放，第 0 帧和压在画面上的字幕更关键 |

## Status and exit codes · 状态和退出码

`pass` (0) · `fail` (2) · `unmeasured` (3: something could not be checked — not a pass). Exit code 1 is reserved for crashes.

`pass`（0）通过 · `fail`（2）有问题 · `unmeasured`（3）有该查但查不了的项，不算通过。退出码 1 留给脚本自己出错。只有加了 `--strict` 才按状态给退出码。

## License · 许可

MIT (see `LICENSE`). Platform UI zone data from [reelsmith/safezone](https://github.com/reelsmith/safezone), MIT — see `THIRD_PARTY.md`.

MIT 许可（见 `LICENSE`）。平台遮挡区数据来自 [reelsmith/safezone](https://github.com/reelsmith/safezone)，同为 MIT 许可，见 `THIRD_PARTY.md`。
