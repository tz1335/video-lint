"""旁注导入 / sidecar import：从现成的字幕和剪辑表生成 video-lint 旁注，不用写代码也能跑全套检查。

  video-lint-import 成片.mp4 --srt 字幕.srt [--edl 剪辑.edl] [-o sidecar.json]
  python -I sidecar_import.py 成片.mp4 --srt 字幕.srt …        （不安装时）

- 帧率、分辨率、时长用 ffprobe 从成片读（也可以 --fps 指定）
- 字幕：.srt / .vtt / .ass（和 lint.py --captions 同一个解析器）→ captions
- 剪辑表：CMX 3600 EDL（Premiere / Resolve / Final Cut 都能导出）→ shots；只取视频轨，
  录制时间码减去第一条事件的起点（时间线常从 01:00:00:00 开始），片段名来自「* FROM CLIP NAME」
没有剪辑表也能跑：lint 会自己找硬切，只是报告里没有镜头名。

Generates a video-lint sidecar from an existing subtitle file (and optionally a CMX 3600 EDL),
reading fps / size / duration from the video with ffprobe. Exit code 0 = written, 2 = bad input.
"""
import argparse
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

try:  # 装成包时
    from .lint import parse_captions
except ImportError:  # 直接跑脚本时（lint.py 在同一目录）
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lint import parse_captions  # noqa: E402

FORMAT = 'video-lint-sidecar/0'
TC = re.compile(r'(\d{1,2}):(\d{2}):(\d{2})[:;.](\d{2,3})')
EVENT = re.compile(r'^\s*(\d{1,6})\s+(\S+)\s+(\S+)\s+(\S+)(?:\s+\d+)?\s+' + r'\s+'.join([r'(\d{1,2}:\d{2}:\d{2}[:;.]\d{2,3})'] * 4))


def probe(video):
    """ffprobe → {'fps', 'width', 'height', 'durationFrames'}"""
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
           'stream=width,height,avg_frame_rate,r_frame_rate,nb_frames:format=duration', '-of', 'json', str(video)]
    try:
        info = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as e:
        raise SystemExit(f'ffprobe 读不了 {video}：{e}（要先装 ffmpeg / install ffmpeg first）')
    s = (info.get('streams') or [{}])[0]
    rate = s.get('avg_frame_rate') if s.get('avg_frame_rate') not in (None, '0/0') else s.get('r_frame_rate', '30/1')
    fps = float(Fraction(rate))
    dur = float((info.get('format') or {}).get('duration') or 0)
    n = int(s['nb_frames']) if str(s.get('nb_frames', '')).isdigit() else round(dur * fps)
    return {'fps': round(fps, 3), 'width': s.get('width'), 'height': s.get('height'), 'durationFrames': n}


def tc_frames(tc, fps):
    """时间码 → 帧。分号（丢帧）按名义帧率数，和大多数 NLE 导出的录制时间一致到 ±2 帧；毫秒格式也认"""
    h, m, s, f = TC.match(tc).groups()
    base = (int(h) * 3600 + int(m) * 60 + int(s)) * round(fps)
    return base + (round(int(f) / 1000 * fps) if len(f) == 3 else int(f))


def parse_edl(path, fps):
    """CMX 3600 EDL → [{'name', 'from', 'to'}]（帧，从 0 开始）。只要视频轨（V / V1 / B），音频事件跳过"""
    events, last = [], None
    for line in Path(path).read_text(encoding='utf-8-sig', errors='replace').splitlines():
        m = EVENT.match(line)
        if m:
            num, reel, track, _kind, _si, _so, ri, ro = m.groups()
            last = None
            if track.upper().startswith(('V', 'B')):
                last = {'name': '', 'reel': reel, 'from': tc_frames(ri, fps), 'to': tc_frames(ro, fps), 'event': int(num)}
                events.append(last)
            continue
        m = re.match(r'^\s*\*\s*FROM CLIP NAME:\s*(.+?)\s*$', line, re.I)
        if m and last is not None and not last['name']:
            last['name'] = m.group(1)
    if not events:
        return []
    events.sort(key=lambda e: (e['from'], e['event']))
    # 同一段录制时间上的转场会写成两条（A 片段出、B 片段入）：留后一条
    merged = []
    for e in events:
        if merged and e['from'] == merged[-1]['from']:
            merged[-1] = e
        else:
            merged.append(e)
    zero = merged[0]['from']
    shots = []
    for i, e in enumerate(merged):
        name = e['name'] or (e['reel'] if e['reel'].upper() not in ('AX', 'BL', 'BLACK') else '') or f'shot{i + 1:03d}'
        shots.append({'name': name, 'from': e['from'] - zero, 'to': e['to'] - zero, 'source': {'file': Path(path).name, 'line': e['event']}})
    return shots


def build(video, srt=None, edl=None, fps=None, platform=None, sid=None):
    meta = probe(video) if video else {}
    if fps:
        meta['fps'] = fps
    if not meta.get('fps'):
        raise SystemExit('不知道帧率：给成片或者 --fps')
    sc = {'format': FORMAT, 'id': sid or (Path(video).stem if video else 'video'), **meta, 'sources': {}}
    notes = []
    if srt:
        sc['captions'] = parse_captions(srt, meta['fps'])
        sc['sources']['captions'] = {'file': Path(srt).name}
        if not sc['captions']:
            notes.append(f'{Path(srt).name} 里没认出字幕 / no cues found')
    if edl:
        shots = parse_edl(edl, meta['fps'])
        n = meta.get('durationFrames')
        if n and shots and shots[-1]['to'] > n + 2:
            notes.append(f'EDL 比成片长 {(shots[-1]["to"] - n) / meta["fps"]:.1f} 秒：是不是导错了时间线 / EDL longer than video')
            shots = [dict(s, to=min(s['to'], n)) for s in shots if s['from'] < n]
        sc['shots'] = shots
        sc['sources']['edl'] = {'file': Path(edl).name}
    if platform:
        sc['platform'] = platform
    if notes:
        sc['notes'] = notes
    return sc


def main(argv=None):
    ap = argparse.ArgumentParser(description='从字幕 / EDL 生成 video-lint 旁注 / build a video-lint sidecar from subtitles and an EDL')
    ap.add_argument('video', nargs='?', help='成片（读帧率、分辨率、时长）')
    ap.add_argument('--srt', '--captions', dest='srt', help='字幕 .srt / .vtt / .ass')
    ap.add_argument('--edl', help='CMX 3600 EDL：镜头表')
    ap.add_argument('--fps', type=float, help='帧率（不给就从成片读）')
    ap.add_argument('--platform', choices=['douyin', 'tiktok', 'reels', 'shorts', 'none'])
    ap.add_argument('--id', help='旁注 id（默认是成片文件名）')
    ap.add_argument('-o', '--out', help='写到这个文件；不给就打印到标准输出')
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if not a.srt and not a.edl:
        ap.error('至少给 --srt 或 --edl 之一 / give --srt and/or --edl')
    for p in (a.video, a.srt, a.edl):
        if p and not Path(p).is_file():
            print(f'找不到文件 / not found: {p}', file=sys.stderr)
            sys.exit(2)
    sc = build(a.video, a.srt, a.edl, a.fps, a.platform, a.id)
    text = json.dumps(sc, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(text, encoding='utf-8')
        print(f'旁注 → {a.out}：{len(sc.get("captions", []))} 条字幕、{len(sc.get("shots", []))} 个镜头', file=sys.stderr)
    else:
        print(text)


if __name__ == '__main__':
    main()
