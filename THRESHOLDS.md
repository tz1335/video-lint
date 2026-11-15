# Default thresholds · 默认阈值

Every number video-lint judges with, where it comes from, and how much evidence is behind it. Be suspicious of anything marked **uncalibrated**: it is a reasonable default, not a measured one. Override levels per project with `[levels]` / `ignore` in `.video-lint.toml`.

video-lint 下判断用到的每个数、出处、有多少证据。标了 **未校准** 的只是一个说得过去的默认值，不是量出来的。项目里可以用 `.video-lint.toml` 的 `[levels]` / `ignore` 改级别或关掉。

**Source types · 出处类型**

- **spec** · 规范 — a published standard or style guide · 公开的标准或规范
- **eng** · 工程 — an engineering estimate (codec noise, typical stereo) · 工程经验值（编码噪声、普通立体声）
- **author** · 作者 — the original author's own published videos and their platform analytics (N = number of videos). With N ≤ 8 nothing here is statistically significant; it only shows the direction · 原作者自己发布的视频和后台数据（N = 视频数）。N ≤ 8 时没有一条是统计显著的，只能看方向
- **data** · 数据 — third-party measured data · 第三方实测数据

Significance: Spearman rank correlation against 2-second bounce rate; with N = 6 a two-sided p < 0.05 needs |ρ| ≥ 0.886. · 显著性：和 2 秒跳出率的 Spearman 秩相关；N = 6 时双侧 p < 0.05 要 |ρ| ≥ 0.886。

| Check · 检查 | Threshold · 阈值 | Source · 出处 | N | Significant? · 显著 | Status · 状态 |
|---|---|---|---|---|---|
| `open.frame0` · `open.rule1` | 99.5th-percentile luma < 24 → ✗ (near black); < 0.5% bright pixels → ✗ · 第 0 帧近乎全黑、亮像素 < 0.5% ✗ | author: one video opened on a near-black glow and lost 40% in 2 s; across 6 videos bright-pixel share vs bounce ρ = +0.09 · 作者：一期开头只有暗光晕，2 秒跳出 40%；6 期上亮像素占比和跳出 ρ = +0.09 | 1 / 6 | no · 否 | uncalibrated; kept because a black first frame is cheap to fix · 未校准；黑屏开头改起来便宜，所以保留 |
| `static` | frame difference < 0.025 for ≥ 1 s; in the first 3 s → ⚠ · 帧差 < 0.025 持续 ≥ 1 秒，前 3 秒内 ⚠ | eng: h264 noise ≈ 0.01, slow particle drift 0.03–0.1 · 工程：h264 噪声约 0.01，慢漂移粒子 0.03–0.1 | — | — | uncalibrated · 未校准 |
| `black` | 99.5th-percentile luma < 24 mid-video → ⚠ · 片中黑场 ⚠ | eng | — | — | uncalibrated · 未校准 |
| `flash` | > 3 general or red flashes in any 1 s over ≥ 341×256 px at 1024×768 (≈ 11%) → ✗ · 任意 1 秒超过 3 次大面积闪烁 ✗ | spec: WCAG 2.3.1 / 2.3.2 general-flash and red-flash thresholds | — | — | standard · 规范 |
| `captions.speed` | > 9 CJK chars/s or > 20 Latin chars/s, or < 5/6 s on screen → ⚠ · 中文 > 9 字/秒、英文 > 20 字符/秒、停留 < 5/6 秒 ⚠ | spec: Netflix Timed Text Style Guide (Simplified Chinese 9 cps, English 20 cps, min duration 5/6 s) | — | — | standard · 规范 |
| `open.rule2` | largest text on frame 0 ≥ 4% of video width → ✓, < 2.5% → ✗ · 第 0 帧最大字 ≥ 视频宽 4% 通过、< 2.5% ✗ | author heuristic (≈ 16 pt on a phone); back-tested on 7 videos, ρ = +0.46 on 6 (direction does not support "bigger is better") · 作者经验值（手机上约 16 号字）；7 期回测，6 期 ρ = +0.46（方向不支持「越大越好」） | 6 | no · 否 | uncalibrated; needs ≥ 20 videos · 未校准，要 20 期以上 |
| `open.read` | words on screen in the first 2 s > 3 × (2 s × 9 chars/s) = 54 → ⚠ · 前 2 秒同屏字数超过 54 字 ⚠ | author: ρ = +0.64 on 6 videos; 18 chars flagged 7 of 8 videos, so loosened to 3× · 作者：6 期 ρ = +0.64；按 18 字 8 期里 7 期都超，放宽到 3 倍 | 6 | no · 否 | uncalibrated · 未校准 |
| `open.rule4/5` | a hook in the top half of a portrait video; a number within 8 s · 竖屏上半屏有钩子；8 秒内出现数字 | author heuristic from a post-mortem, not measured · 作者复盘经验，没有量过 | — | — | uncalibrated · 未校准 |
| `end.ask` | last screen is a question and readable ≥ 2 s · 片尾停在问句上、看得清 ≥ 2 秒 | eng: a ~10-character question at 9 chars/s plus thinking time; the premise (comments help distribution) is the author's, not measured · 工程：10 字左右的问题按每秒 9 字读完再想一下；「评论有助于推荐」是作者的判断，没量过 | — | — | uncalibrated · 未校准 |
| `beats.cuts` | hard cut > ±1.5 frames from the beat grid → ⚠ / ✗ · 硬切离拍点超过 ±1.5 帧 | author convention for music-driven edits · 作者卡拍剪辑的习惯值 | — | — | uncalibrated · 未校准 |
| `audio.sfx` (no stem) | effect rises < 4 dB full-band and < 6 dB at 1.2–5 kHz over the moment before → ⚠ (only with `--loud`, otherwise info) · 比前一刻全频高不到 4 dB 且 1.2–5 kHz 高不到 6 dB | author: tuned on two music-driven styles; false positives when an effect lands on a downbeat · 作者：在两种卡配乐的风格上调的，音效落在重拍上会误报 | — | — | uncalibrated · 未校准 |
| `audio.sfx` (stem) | effect-to-music ratio ≥ −3 dB clear; < −10 dB (full band and high band) nearly inaudible; gain plan = dB needed to reach −3 dB · 音效 / 配乐能量比 ≥ −3 dB 听得清、< −10 dB 几乎听不见；补多少 dB 按到 −3 dB 算 | author: −10 dB confirmed by ear on 1 video (41 of 44 effects judged inaudible were inaudible); −3 dB not yet verified · 作者：−10 dB 在 1 期上耳听确认；−3 dB 还没验证 | 1 | — | partly checked by ear · 部分耳听确认 |
| `mono` | mono fold-down drops > 4.5 dB overall or > 9 dB in any 0.5 s → ⚠ · 混成单声道整片小 4.5 dB 以上或某半秒小 9 dB 以上 | eng: ordinary stereo drops ≈ 3 dB | — | — | uncalibrated · 未校准 |
| `audio.jump` | ≥ 15 dB louder within 1 s → ⚠ (unweighted level) · 1 秒内响 15 dB 以上 | spec-inspired: EBU R128 guidance against loudness jumps; the 15 dB figure and the unweighted approximation are this tool's · 参照 EBU R128 对响度跳变的建议；15 dB 和不加权近似是本工具定的 | — | — | approximation · 近似 |
| `audio.silence` | ≥ 1 s below −50 dBFS mid-video → ⚠ · 片中 ≥ 1 秒低于 −50 dBFS | eng | — | — | uncalibrated · 未校准 |
| `audio.loudness` | true peak > −1 dBFS ⚠, > 0 ✗; integrated LUFS ±1 of `--lufs` · 真峰值 > −1 ⚠、> 0 ✗；整片响度 ±1 | spec: EBU R128 / ITU-R BS.1770 (−1 dBTP); the LUFS target is yours · 规范；响度目标自己定 | — | — | standard · 规范 |
| `text.contrast` | large text < 3:1 → ⚠; 3:1–4.5:1 noted · 大字对比度 < 3:1 ⚠ | spec: WCAG 1.4.3 | — | — | standard · 规范 |
| `text.zones` | at least one glyph covered by a platform UI zone → ✗; edge overlap ≥ 10% of the box → ⚠ · 至少盖住一个字 ✗、擦边 ⚠ | data: [reelsmith/safezone](https://github.com/reelsmith/safezone) zone rectangles, averaged over recent phones; Douyin reuses TikTok's · 遮挡区数据来自 reelsmith/safezone | — | — | approximate data · 近似数据 |
| `text.glyphs` (`--font`) | any caption character missing from every given font → ⚠ · 字幕里有字所有字体都没有 ⚠ | exact: read from the font's cmap (fontTools) · 精确：读字体 cmap | — | — | exact · 精确 |
| `cover.*.feed` | tile width 130 CSS px (portrait, 3-column grid) / 180 (landscape); text < 9 CSS px tall in the tile → ⚠ · 封面格子宽 130 / 180 CSS 像素，字高 < 9 CSS 像素 ⚠ | eng: estimated from screenshots, not measured per device · 工程：看截图估的，没有逐机型实测 | — | — | uncalibrated · 未校准 |
| `spec` | codec, pixel format, fps, resolution, duration, audio — `video_lint/specs.json` · 上传规格 | spec: each platform's public upload guidelines (as of 2026) · 各平台公开的上传说明 | — | — | standard, may go stale · 规范，可能过时 |
| `retention.why` | look 1 s before each drop window · 每个掉人窗口往前多看 1 秒 | eng: reaction time | — | — | uncalibrated · 未校准 |
| OCR box → glyph height | × 0.74 | author: median OCR box / glyph height = 1.36 over 67 text lines in 1 video · 作者：1 期 67 行对照 | 1 | — | measured once · 量过一次 |

Want to help calibrate? Run video-lint on your own published videos, put the platform numbers in the sidecar's `performance`, and open an issue with the `features` block of `report.json` (no video needed). · 想帮忙校准：用 video-lint 查你自己发过的视频，把后台数字写进旁注的 `performance`，在 issue 里贴 `report.json` 的 `features`（不用传视频）。
