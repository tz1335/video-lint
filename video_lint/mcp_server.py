"""video-lint 的 MCP 服务：让 Claude Code、Cursor 这类 agent 把成片质检当工具直接调。

  pipx install "video-lint[mcp,ocr] @ git+https://github.com/tz1335/video-lint"
  claude mcp add video-lint -- video-lint-mcp

工具：
  lint_video      质检一个视频，返回整体状态、要处理的问题（✗ ⚠ ?）和报告路径（report.md / report.html / report.json）
  list_platforms  能用的 --platform 和它们的遮挡区、规格数据
它在子进程里跑同目录的 lint.py，所以和命令行的结论完全一样。
"""
import json
import subprocess
import sys
from pathlib import Path

try:  # mcp 2.x 把 FastMCP 改名成了 MCPServer，用法一样
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

HERE = Path(__file__).resolve().parent
LINT = HERE / 'lint.py'

mcp = _Server('video-lint')


@mcp.tool()
def lint_video(
    video: str,
    platform: str | None = None,
    lufs: float | None = None,
    captions: str | None = None,
    sidecar: str | None = None,
    sfx_audio: str | None = None,
    out: str | None = None,
    ocr: bool = True,
    flash: bool = True,
) -> dict:
    """Check a finished short video (mp4 / mov / mkv) before publishing: first frame and first two seconds,
    black / frozen stretches, flashing (WCAG 2.3.1), upload spec, text under platform UI zones, cut-on-beat,
    loudness, buried sound effects, caption reading speed.

    Args:
        video: absolute path to the video file.
        platform: douyin, tiktok, reels, shorts or none — UI zones and upload spec to check against.
        lufs: integrated loudness target, e.g. -14; loudness is only judged when given.
        captions: path to an .srt / .vtt / .ass file, for reading speed and opening-line rules.
        sidecar: path to a video-lint sidecar JSON (shots, hits, beats, sfx, captions).
        sfx_audio: path to a sound-effects-only stem, to measure effects against the music.
        out: report folder; defaults to <video>-lint/ next to the video.
        ocr: find text with OCR when there is no text layer (needs the [ocr] extra).
        flash: run the flash safety check.

    Returns status (pass / fail / unmeasured), a one-line headline, every fail / warn / skip check with its
    detail, and the paths of report.md, report.html (self-contained, shareable) and report.json.
    Fix fail items first; warn items need a judgement; unmeasured means something could not be checked.
    """
    v = Path(video).expanduser().resolve()
    if not v.is_file():
        return {'error': f'找不到视频：{v}'}
    o = Path(out).expanduser().resolve() if out else v.with_name(v.stem + '-lint')
    cmd = [sys.executable, '-I', str(LINT), str(v), '--out', str(o)]
    for flag, val in (('--platform', platform), ('--lufs', lufs), ('--captions', captions), ('--sidecar', sidecar), ('--sfx-audio', sfx_audio)):
        if val is not None:
            cmd += [flag, str(val)]
    if not ocr:
        cmd.append('--no-ocr')
    if not flash:
        cmd.append('--no-flash')
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    report = o / 'report.json'
    if p.returncode not in (0, 2, 3) or not report.exists():
        return {'error': 'video-lint 运行失败', 'stderr': p.stderr[-2000:]}
    rep = json.loads(report.read_text(encoding='utf-8'))
    s = rep['summary']
    return {
        'status': rep['status'],
        'headline': f'{s["fail"]} fail, {s["warn"]} warn, {s["pass"]} pass, {s["skip"]} unmeasured',
        'problems': [{k: c[k] for k in ('id', 'level', 'title', 'detail')} for c in rep['checks'] if c['level'] in ('fail', 'warn', 'skip')],
        'report_md': str(o / 'report.md'),
        'report_html': rep['images'].get('html'),
        'report_json': str(report),
        'version': rep['version'],
    }


@mcp.tool()
def list_platforms() -> dict:
    """Platforms accepted by lint_video(platform=...), with their UI-zone data source and upload-spec values."""
    zones = json.loads((HERE / 'zones.json').read_text(encoding='utf-8'))
    specs = json.loads((HERE / 'specs.json').read_text(encoding='utf-8'))
    return {
        'platforms': {k: {'name': v['name'], 'zones': k in zones['platforms'], 'spec': v} for k, v in specs['platforms'].items()},
        'zones_source': zones['source'],
        'specs_note': specs['note'],
    }


def main():
    mcp.run()


if __name__ == '__main__':
    main()
