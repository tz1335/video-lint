"""video-lint 的测试：用 ffmpeg 现场合成已知答案的小视频，跑 lint.py，核对 report.json。

  python -m pytest scripts/video-lint/tests -q
需要 ffmpeg / ffprobe 在 PATH 里；每个视频只有几秒、320 像素宽，整套几十秒跑完。
"""
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

HERE = Path(__file__).resolve().parent
LINT = HERE.parent / 'lint.py'
sys.path.insert(0, str(HERE.parent))
import lint  # noqa: E402

FPS = 30


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True)


def make_video(path, lum, w=320, h=180, seconds=4, audio=None):
    """lum 是 ffmpeg geq 的亮度表达式（N 是帧号）；audio 是 aevalsrc 表达式或 None（不带音轨）"""
    args = ['-f', 'lavfi', '-i', f"color=c=gray:s={w}x{h}:r={FPS}:d={seconds},format=gray,geq=lum='{lum}'"]
    if audio:
        args += ['-f', 'lavfi', '-i', f"aevalsrc='{audio}':s=48000:d={seconds}"]
    args += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18']
    if audio:
        args += ['-c:a', 'aac', '-b:a', '192k', '-shortest']
    ffmpeg(*args, str(path))
    return path


def write_wav(path, samples, sr=48000):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def lint_report(video, out, *args, sidecar=None):
    cmd = [sys.executable, '-I', str(LINT), str(video), '--out', str(out), '--strict', *args]
    if sidecar is not None:
        sc = Path(out).with_suffix('.sidecar.json')
        sc.write_text(json.dumps(sidecar, ensure_ascii=False), encoding='utf-8')
        cmd += ['--sidecar', str(sc)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert p.returncode in (0, 2, 3), p.stderr
    rep = json.loads((Path(out) / 'report.json').read_text(encoding='utf-8'))
    rep['exit'] = p.returncode
    rep['by'] = {c['id']: c for c in rep['checks']}
    return rep


# 一个有纹理、会动的画面（亮度 80–160），第 0 帧就有内容
MOVING = '120+40*sin(X/9+N/3)'
TONE = '0.2*sin(2*PI*440*t)'


# —— 纯函数 ——

def test_platform_zones_douyin_reuses_tiktok():
    d, t = lint.platform_zones('douyin'), lint.platform_zones('tiktok')
    assert d['zones'] == t['zones'] and d['safe'] == t['safe']
    assert '抖音' in d['name']
    assert lint.platform_zones('nope') is None


def test_merge_lines_joins_pieces_of_one_line_only():
    a, b, c = [0.1, 0.50, 0.3, 0.53], [0.4, 0.505, 0.6, 0.535], [0.1, 0.70, 0.5, 0.73]
    assert lint.merge_lines([a, b, c]) == [[0.1, 0.5, 0.6, 0.535], c]


def test_magenta_matches_dimmed_magenta_not_gold_or_gray():
    px = np.array([[[255, 0, 255], [110, 20, 110], [230, 180, 60], [128, 128, 128]]], np.uint8)
    assert lint.magenta(px).tolist() == [[True, True, False, False]]


def test_retention_points_end_on_completion_and_drop_late_manual_points():
    pts = lint.retention_points({'bounce2s': 0.4, 'retention5s': 0.3, 'completion': 0.05, 'retention': [[10, 0.2], [19.5, 0.12]]}, 20)
    assert pts[0] == (0.0, 1.0)
    assert (2.0, pytest.approx(0.6)) in [(t, pytest.approx(v)) for t, v in pts]
    assert pts[-1] == (20, 0.05)
    assert all(t < 18 for t, _ in pts[:-1])  # 19.5 秒的手抄点被去掉


def test_runs_and_hard_cuts():
    assert lint.runs([0, 1, 1, 1, 0, 1, 0], 2) == [(1, 4)]
    diff = np.full(60, 0.3)
    diff[30] = 40
    assert lint.hard_cuts(diff) == [30]


# —— 整条流程：合成视频 → report.json ——

def test_clean_video_passes(tmp_path):
    v = make_video(tmp_path / 'clean.mp4', MOVING, audio=TONE)
    r = lint_report(v, tmp_path / 'out')
    assert r['by']['open.frame0']['level'] == 'pass'
    assert r['by']['black']['level'] == 'pass'
    assert 'audio.loudness' in r['by']
    assert r['status'] == 'pass' and r['exit'] == 0
    for f in ('report.md', 'frame0.jpg', 'curve.png', 'sheet-1.jpg'):
        assert (tmp_path / 'out' / f).exists()


def test_black_first_frame_fails_once(tmp_path):
    v = make_video(tmp_path / 'black0.mp4', f'if(eq(N,0),0,{MOVING})', audio=TONE)
    r = lint_report(v, tmp_path / 'out')
    assert r['by']['open.frame0']['level'] == 'fail'
    assert 'open.black' not in r['by']  # 第 0 帧已经判过，「前 2 秒黑帧」不重复报
    assert r['status'] == 'fail' and r['exit'] == 2


def test_no_audio_is_unmeasured_not_pass(tmp_path):
    v = make_video(tmp_path / 'silent.mp4', MOVING)
    r = lint_report(v, tmp_path / 'out')
    assert r['by']['audio']['level'] == 'skip'
    assert r['status'] == 'unmeasured' and r['exit'] == 3


def test_hit_darken_freeze_burst_and_off_beat_cut(tmp_path):
    # 第 45 帧切在拍上，第 62 帧故意偏 2 帧；70 帧起收暗，87–89 帧静止，第 90 帧爆亮
    lum = ("if(gte(N,90),215+20*sin(X/9+N/3),st(0,min(N,87));"
           "clip((120+40*sin(X/9+ld(0)/3+3*gte(ld(0),45)+3*gte(ld(0),62))+30*sin(Y/7)*gte(ld(0),45))*(1-0.035*max(0,ld(0)-70)),0,255))")
    v = make_video(tmp_path / 'hit.mp4', lum.replace(',', '\\,'), audio=TONE)
    sc = {'fps': FPS, 'durationFrames': 120, 'shots': [{'name': 'a', 'from': 0, 'to': 45}, {'name': 'b', 'from': 45, 'to': 90}, {'name': 'c', 'from': 90, 'to': 120}],
          'hits': [{'name': 'hit1', 'frame': 90, 'darkFrom': 70, 'freeze': 3}], 'beats': {'period': 0.5, 'offset': 0, 'source': 'test', 'times': [i * 0.5 for i in range(9)]}, 'sfx': [], 'captions': []}
    r = lint_report(v, tmp_path / 'out', sidecar=sc)
    hit = r['by']['hit.hit1']
    assert hit['level'] == 'pass', hit['detail']
    cuts = r['by']['beats.cuts']
    assert cuts['data']['offBeat'] == [62]
    assert 45 in cuts['data']['cuts'] and 90 in cuts['data']['cuts']


def test_hit_without_freeze_warns(tmp_path):
    lum = "if(gte(N,90),215+20*sin(X/9+N/3),clip((120+40*sin(X/9+N/3))*(1-0.035*max(0,N-70)),0,255))"
    v = make_video(tmp_path / 'nofreeze.mp4', lum.replace(',', '\\,'), audio=TONE)
    sc = {'fps': FPS, 'durationFrames': 120, 'shots': [], 'hits': [{'name': 'hit1', 'frame': 90, 'darkFrom': 70, 'freeze': 3}], 'sfx': [], 'captions': []}
    r = lint_report(v, tmp_path / 'out', sidecar=sc)
    assert r['by']['hit.hit1']['level'] == 'warn'
    assert '静止 ✗' in r['by']['hit.hit1']['detail']


def test_loudness_target_and_true_peak(tmp_path):
    loud = make_video(tmp_path / 'loud.mp4', MOVING, audio='0.99*sin(2*PI*440*t)')
    r = lint_report(loud, tmp_path / 'o1')
    assert r['by']['audio.loudness']['level'] in ('warn', 'fail')  # 真峰值贴着 0 dBFS
    quiet = make_video(tmp_path / 'quiet.mp4', MOVING, audio='0.01*sin(2*PI*440*t)')
    r = lint_report(quiet, tmp_path / 'o2', '--lufs', '-14')
    assert r['by']['audio.loudness']['level'] == 'fail'  # 比目标低 20 dB 以上


@pytest.mark.parametrize('y0,expect', [(0.85, 'fail'), (0.40, 'pass')])
def test_text_layer_in_douyin_zone(tmp_path, y0, expect):
    w, h = 270, 480
    v = make_video(tmp_path / 'tall.mp4', MOVING, w=w, h=h, seconds=3, audio=TONE)
    text = tmp_path / 'text'
    text.mkdir()
    frames = [0, 15, 30, 45, 59]
    for f in frames:
        im = np.array(Image.new('RGB', (w, h), (120, 120, 120)))
        im[int(y0 * h):int(y0 * h) + 14, int(0.2 * w):int(0.7 * w)] = (255, 0, 255)
        Image.fromarray(im).save(text / f'f{f}.png')
    (text / 'frames.json').write_text(json.dumps({'frames': frames, 'scale': 1, 'width': w, 'height': h}), encoding='utf-8')
    r = lint_report(v, tmp_path / 'out', '--text-dir', str(text), '--platform', 'douyin')
    assert r['by']['text.zones']['level'] == expect
    assert r['features']['frame0MaxText'] == pytest.approx(14 / h * h / w, rel=0.25)


@pytest.mark.parametrize('sfx_gain,expect', [(0.03, 'warn'), (1.0, 'pass')])
def test_sfx_buried_under_music_by_stem(tmp_path, sfx_gain, expect):
    sr, sec = 48000, 3
    t = np.arange(sr * sec) / sr
    music = 0.3 * np.random.default_rng(1).standard_normal(len(t)) * 0.5
    sfx = np.zeros_like(t)
    for at in (1.0, 2.0):
        i = int(at * sr)
        sfx[i:i + 4800] = 0.8 * np.sin(2 * math.pi * 2000 * t[:4800]) * np.exp(-t[:4800] * 20)
    write_wav(tmp_path / 'mix.wav', music + sfx_gain * sfx)
    write_wav(tmp_path / 'sfx.wav', sfx)
    ffmpeg('-f', 'lavfi', '-i', f"color=c=gray:s=320x180:r={FPS}:d={sec},format=gray,geq=lum='{MOVING}'", '-i', str(tmp_path / 'mix.wav'),
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-shortest', str(tmp_path / 'v.mov'))
    sc = {'fps': FPS, 'shots': [], 'hits': [], 'captions': [], 'sfx': [{'name': 'ding', 'frame': 30, 'volume': 1}, {'name': 'ding', 'frame': 60, 'volume': 1}]}
    r = lint_report(tmp_path / 'v.mov', tmp_path / 'out', '--sfx-audio', str(tmp_path / 'sfx.wav'), sidecar=sc)
    assert r['by']['audio.sfx']['level'] == expect
    # 配乐是随机噪声，和音效有一点偶然相关；音效很轻时增益估计会差零点零几
    assert r['by']['audio.sfx']['data']['gain'] == pytest.approx(sfx_gain, rel=0.1, abs=0.02)
