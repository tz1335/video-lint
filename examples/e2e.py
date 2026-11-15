"""端到端示例 / end-to-end example：合成一段带字幕的小视频 → SRT 生成旁注 → lint → 读 report.json。

  python examples/e2e.py [工作目录]        （在仓库根目录跑；装过 pip install . 也行）

只需要 ffmpeg / ffprobe 和 requirements.txt 里的依赖。退出码 0 = 整条链路跑通（不代表视频没问题）。
Synthesizes a 6-second portrait clip with ffmpeg, writes an SRT, builds a sidecar with
video-lint-import, runs video-lint on it and checks that the report has the caption checks.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓库根目录（video_lint 包在这里）
SRT = """1
00:00:00,000 --> 00:00:02,000
早上起不来，真的是懒吗？

2
00:00:02,000 --> 00:00:04,000
先看一个实验

3
00:00:04,000 --> 00:00:06,000
你是哪一种？
"""


def run(*args, ok=(0,)):
    env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get('PYTHONPATH', ''), PYTHONIOENCODING='utf-8')
    p = subprocess.run(args, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if p.returncode not in ok:
        sys.exit(f'失败 / failed ({p.returncode}): {" ".join(map(str, args))}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}')
    return p


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')  # Windows 控制台默认不是 UTF-8
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix='video-lint-e2e-'))
    work.mkdir(parents=True, exist_ok=True)
    video, srt, sidecar, out = work / 'clip.mp4', work / 'clip.srt', work / 'clip.sidecar.json', work / 'clip-lint'
    # 竖屏 360×640、30 fps、6 秒：会动的测试图 + 一个 440 Hz 的音
    run('ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=360x640:r=30:d=6',
        '-f', 'lavfi', '-i', 'sine=f=440:d=6:sample_rate=48000', '-ac', '2',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(video))
    srt.write_text(SRT, encoding='utf-8')
    run(sys.executable, '-m', 'video_lint.sidecar_import', str(video), '--srt', str(srt), '--platform', 'douyin', '-o', str(sidecar))
    sc = json.loads(sidecar.read_text(encoding='utf-8'))
    assert sc['fps'] == 30 and sc['durationFrames'] == 180 and len(sc['captions']) == 3, sc
    # --strict：0 通过、2 有问题、3 有项查不了（比如没装 OCR）；1 才是工具自己出错
    p = run(sys.executable, '-m', 'video_lint', str(video), '--sidecar', str(sidecar), '--out', str(out), '--no-ocr', '--strict', ok=(0, 2, 3))
    rep = json.loads((out / 'report.json').read_text(encoding='utf-8'))
    ids = {c['id'] for c in rep['checks']}
    for need in ('captions.speed', 'end.ask', 'open.frame0'):
        assert need in ids, f'报告里缺 {need}：{sorted(ids)}'
    ask = next(c for c in rep['checks'] if c['id'] == 'end.ask')
    assert ask['level'] == 'pass', ask
    print(f'跑通 / ok：{len(rep["checks"])} 项检查，整体 {rep["status"]}（退出码 {p.returncode}）→ {out / "report.json"}')


if __name__ == '__main__':
    main()
