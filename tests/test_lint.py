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
LINT = HERE.parent / 'lint.py'  # 原工程里是分析器本身；独立仓库里是调用 video_lint 包的小入口
sys.path.insert(0, str(HERE.parent))
try:  # 独立仓库：代码在 video_lint 包里
    from video_lint import lint  # noqa: E402
except ImportError:  # 原工程：lint.py 就在上一级
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
    # a、b 同一行、隔 0.04（不到两个字高）→ 合成一行；c 是下一行；d 和 a 同高但隔了半个画面 → 不合
    a, b, c, d = [0.1, 0.50, 0.3, 0.53], [0.34, 0.505, 0.6, 0.535], [0.1, 0.70, 0.5, 0.73], [0.85, 0.50, 0.95, 0.53]
    assert lint.merge_lines([a, b, c, d]) == [[0.1, 0.5, 0.6, 0.535], d, c]


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


# —— OCR 兜底（没装 rapidocr-onnxruntime 就跳过）——

def _has_ocr():
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_ocr(), reason='没装 rapidocr-onnxruntime')
@pytest.mark.parametrize('y0,expect', [(0.83, 'fail'), (0.40, 'pass')])
def test_ocr_finds_text_without_text_layer(tmp_path, y0, expect):
    from PIL import ImageDraw, ImageFont
    w, h = 540, 960
    im = Image.new('RGB', (w, h), (30, 30, 34))
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=48)
    d.text((60, int(y0 * h)), 'HELLO 2026', fill=(250, 250, 250), font=font)
    im.save(tmp_path / 'still.png')
    ffmpeg('-loop', '1', '-i', str(tmp_path / 'still.png'), '-f', 'lavfi', '-i', f"aevalsrc='{TONE}':s=48000:d=3", '-t', '3', '-r', str(FPS),
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(tmp_path / 'v.mp4'))
    r = lint_report(tmp_path / 'v.mp4', tmp_path / 'out', '--platform', 'douyin')
    assert r['by']['text.zones']['level'] == expect
    assert 'OCR' in r['by']['text.zones']['detail']
    assert r['features']['frame0MaxText'] > 0.03


def test_no_ocr_flag_falls_back_to_info(tmp_path):
    v = make_video(tmp_path / 'v.mp4', MOVING, w=180, h=320, seconds=2, audio=TONE)
    r = lint_report(v, tmp_path / 'out', '--no-ocr')
    assert r['by']['text']['level'] == 'info'


# —— 闪烁安全（WCAG 2.3.1）——

def make_rgb_video(path, frames):
    """frames: (n, h, w, 3) uint8，逐帧原样编码（无损，免得压缩把闪烁抹平）"""
    n, h, w, _ = frames.shape
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(FPS), '-i', '-',
                    '-c:v', 'libx264rgb', '-crf', '0', str(path)], input=frames.tobytes(), check=True)
    return path


def flicker(hz, color=(255, 255, 255), area=1.0, seconds=3, w=160, h=90):
    """前景色和黑色交替，每秒 hz 次完整闪烁；area 是闪烁区域占画面的比例（左上角一块）"""
    n = seconds * FPS
    half = FPS / hz / 2
    frames = np.zeros((n, h, w, 3), np.uint8)
    frames[:] = (40, 40, 40)
    bw, bh = int(w * math.sqrt(area)), int(h * math.sqrt(area))
    for i in range(n):
        if int(i // half) % 2 == 0:
            frames[i, :bh, :bw] = color
        else:
            frames[i, :bh, :bw] = (0, 0, 0)
    return frames


@pytest.mark.parametrize('hz,area,expect', [(5, 1.0, 'fail'), (2, 1.0, 'pass'), (5, 0.05, 'pass')])
def test_flash_general(tmp_path, hz, area, expect):
    v = make_rgb_video(tmp_path / 'f.mkv', flicker(hz, area=area))
    r = lint_report(v, tmp_path / 'out', '--no-ocr')
    assert r['by']['flash']['level'] == expect, r['by']['flash']['detail']


def test_flash_red_detected_separately(tmp_path):
    v = make_rgb_video(tmp_path / 'red.mkv', flicker(5, color=(230, 10, 10)))
    r = lint_report(v, tmp_path / 'out', '--no-ocr')
    assert r['by']['flash.red']['level'] == 'fail'


def test_slow_fade_is_not_a_flash(tmp_path):
    n = 3 * FPS
    frames = np.zeros((n, 90, 160, 3), np.uint8)
    for i in range(n):  # 3 秒里从黑慢慢变白再变回来，只算两次跳变
        frames[i] = int(255 * (1 - abs(i / (n - 1) * 2 - 1)))
    v = make_rgb_video(tmp_path / 'fade.mkv', frames)
    r = lint_report(v, tmp_path / 'out', '--no-ocr')
    assert r['by']['flash']['level'] == 'pass'
    assert r['by']['flash']['data']['maxTransitionsPerSecond'] <= 2


# —— 上传规格 ——

def test_spec_pass_and_warnings(tmp_path):
    v = make_video(tmp_path / 'ok.mp4', MOVING, w=320, h=180, seconds=2, audio=TONE)
    r = lint_report(v, tmp_path / 'o1', '--no-ocr', '--no-flash')
    issues = r['by']['spec']['data']['issues']
    assert all('像素格式' not in x and '编码' not in x for x in issues)  # h264 + yuv420p 没问题
    assert any('短边' in x for x in issues)  # 180 像素太小
    r = lint_report(v, tmp_path / 'o2', '--no-ocr', '--no-flash', '--platform', 'tiktok')
    assert any('画幅 16:9' in x for x in r['by']['spec']['data']['issues'])


def test_spec_flags_non_420_pixel_format(tmp_path):
    v = make_rgb_video(tmp_path / 'rgb.mkv', flicker(1, seconds=1))
    r = lint_report(v, tmp_path / 'out', '--no-ocr', '--no-flash')
    assert r['by']['spec']['level'] == 'warn'
    assert any('像素格式' in x for x in r['by']['spec']['data']['issues'])


# —— 字幕导入和阅读速度 ——

BS = chr(92)  # 反斜杠：ASS 的样式标签和换行都用它


def test_parse_srt_vtt_ass(tmp_path):
    (tmp_path / 'a.srt').write_text('1\n00:00:00,500 --> 00:00:02,000\n<i>你好</i>\n世界\n\n2\n00:00:02,000 --> 00:00:03,250\nHi\n', encoding='utf-8')
    (tmp_path / 'a.vtt').write_text('WEBVTT\n\n00:00.500 --> 00:02.000 line:0\n你好世界\n', encoding='utf-8')
    (tmp_path / 'a.ass').write_text('[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                                    'Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,{' + BS + 'b1}你好' + BS + 'N世界\n', encoding='utf-8')
    srt = lint.parse_captions(tmp_path / 'a.srt', 30)
    assert srt == [{'from': 15, 'to': 60, 'text': '你好 世界'}, {'from': 60, 'to': 98, 'text': 'Hi'}]
    assert lint.parse_captions(tmp_path / 'a.vtt', 30) == [{'from': 15, 'to': 60, 'text': '你好世界'}]
    assert lint.parse_captions(tmp_path / 'a.ass', 30) == [{'from': 15, 'to': 60, 'text': '你好 世界'}]


def test_reading_speed_from_srt(tmp_path):
    v = make_video(tmp_path / 'v.mp4', MOVING, seconds=4, audio=TONE)
    (tmp_path / 'c.srt').write_text('1\n00:00:00,000 --> 00:00:02,000\n八个字刚好读得完\n\n'
                                    '2\n00:00:02,000 --> 00:00:03,000\n这一句字幕在一秒里塞了十五个字太快了\n\n'
                                    '3\n00:00:03,000 --> 00:00:03,500\n太短\n', encoding='utf-8')
    r = lint_report(v, tmp_path / 'out', '--no-ocr', '--no-flash', '--captions', str(tmp_path / 'c.srt'))
    sp = r['by']['captions.speed']
    assert sp['level'] == 'warn'
    assert sp['data']['fast'] == 1 and sp['data']['short'] == 1 and len(sp['data']['at']) == 2
    assert '开头规则 2' in r['by']['open.rule2']['title'] if 'open.rule2' in r['by'] else True


# —— 配置文件 .video-lint.toml ——

def test_config_file_sets_defaults_and_ignores_checks(tmp_path):
    v = make_video(tmp_path / 'v.mp4', f'if(eq(N,0),0,{MOVING})', audio=TONE)  # 第 0 帧黑屏 → open.frame0 本来是 ✗
    (tmp_path / '.video-lint.toml').write_text('platform = "tiktok"\nlufs = -14\nno_ocr = true\nignore = ["open.frame0"]\n\n[levels]\n"spec" = "info"\n', encoding='utf-8')
    r = lint_report(v, tmp_path / 'out')
    assert r['by']['open.frame0']['level'] == 'info' and '已忽略' in r['by']['open.frame0']['title']
    assert r['by']['spec']['level'] == 'info'
    assert '目标 -14' in r['by']['audio.loudness']['detail']
    assert 'TikTok' in r['by']['spec']['title']
    assert r['by']['config']['level'] == 'info'
    # 命令行优先于配置文件
    r = lint_report(v, tmp_path / 'out2', '--platform', 'shorts')
    assert 'YouTube Shorts' in r['by']['spec']['title']


def test_config_rejects_unknown_keys(tmp_path):
    v = make_video(tmp_path / 'v.mp4', MOVING, seconds=1, audio=TONE)
    cfg = tmp_path / 'x.toml'
    cfg.write_text('platfrom = "tiktok"\n', encoding='utf-8')
    p = subprocess.run([sys.executable, '-I', str(LINT), str(v), '--config', str(cfg), '--out', str(tmp_path / 'o')], capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert p.returncode != 0 and 'platfrom' in (p.stderr + p.stdout)


# —— 单文件 HTML 报告 ——

def test_html_report_is_self_contained(tmp_path):
    v = make_video(tmp_path / 'v.mp4', MOVING, seconds=2, audio=TONE)
    r = lint_report(v, tmp_path / 'out', '--no-ocr')
    page = (tmp_path / 'out' / 'report.html').read_text(encoding='utf-8')
    assert r['images']['html'].endswith('report.html')
    assert 'data:image/jpeg;base64,' in page and 'data:image/png;base64,' in page  # 联系表和曲线都内嵌了
    assert 'src="sheet' not in page and 'http' not in page.split('<footer>')[0].split('</style>')[1][:200]
    for c in r['checks']:
        assert c['id'] in page


# —— MCP 服务（没装 mcp 就跳过）——

def _has_mcp():
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_mcp(), reason='没装 mcp')
def test_mcp_tools(tmp_path):
    try:
        from video_lint import mcp_server
    except ImportError:
        import mcp_server
    v = make_video(tmp_path / 'v.mp4', f'if(eq(N,0),0,{MOVING})', seconds=2, audio=TONE)
    res = mcp_server.lint_video(str(v), platform='tiktok', ocr=False)
    assert res['status'] == 'fail'
    assert any(p['id'] == 'open.frame0' for p in res['problems'])
    assert Path(res['report_html']).exists()
    assert mcp_server.lint_video(str(tmp_path / 'nope.mp4')) == {'error': f'找不到视频：{(tmp_path / "nope.mp4").resolve()}'}
    plats = mcp_server.list_platforms()['platforms']
    assert {'douyin', 'tiktok', 'reels', 'shorts'} <= set(plats)


# —— 手机外放（单声道）——

def _stereo_video(tmp_path, name, left, right, sr=48000):
    st = np.stack([left, right], axis=1)
    with wave.open(str(tmp_path / f'{name}.wav'), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(st, -1, 1) * 32767).astype(np.int16).tobytes())
    ffmpeg('-f', 'lavfi', '-i', f"color=c=gray:s=320x180:r={FPS}:d=3,format=gray,geq=lum='{MOVING}'", '-i', str(tmp_path / f'{name}.wav'),
           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-shortest', str(tmp_path / f'{name}.mov'))
    return tmp_path / f'{name}.mov'


@pytest.mark.parametrize('kind,expect', [('same', 'pass'), ('wide', 'pass'), ('antiphase', 'warn')])
def test_mono_fold_down(tmp_path, kind, expect):
    sr = 48000
    t = np.arange(sr * 3) / sr
    rng = np.random.default_rng(2)
    a = 0.3 * np.sin(2 * math.pi * 220 * t)
    if kind == 'same':
        left, right = a, a
    elif kind == 'wide':  # 左右互不相关：普通的宽立体声，混成单声道约小 3 dB
        left, right = 0.2 * rng.standard_normal(len(t)), 0.2 * rng.standard_normal(len(t))
    else:  # 一半时间左右反相
        right = np.where(t < 1.5, a, -a)
        left = a
    v = _stereo_video(tmp_path, kind, left, right)
    r = lint_report(v, tmp_path / 'out', '--no-ocr', '--no-flash')
    assert r['by']['mono']['level'] == expect, r['by']['mono']['title']
    if kind == 'antiphase':
        assert r['by']['mono']['data']['segments']  # 反相的那半段被列出来


# —— 和上一版比 ——

def test_compare_lists_fixed_and_new(tmp_path):
    old = make_video(tmp_path / 'v1.mp4', f'if(eq(N,0),0,{MOVING})', audio=TONE)  # 第 0 帧黑屏
    lint_report(old, tmp_path / 'r1', '--no-ocr')
    new = make_video(tmp_path / 'v2.mp4', MOVING, audio=TONE)  # 修好了
    r = lint_report(new, tmp_path / 'r2', '--no-ocr', '--compare', str(tmp_path / 'r1'))
    cmp_ = r['by']['compare']
    assert 'open.frame0' in cmp_['data']['fixed']
    assert cmp_['data']['added'] == []
    assert cmp_['level'] == 'pass'
    # 反过来比：第 0 帧黑屏是新出现的问题
    r = lint_report(old, tmp_path / 'r3', '--no-ocr', '--compare', str(tmp_path / 'r2' / 'report.json'))
    assert 'open.frame0' in r['by']['compare']['data']['added']
    assert r['by']['compare']['level'] == 'warn'


def test_compare_reports_pure():
    old = [{'id': 'a', 'level': 'fail', 'title': 'A'}, {'id': 'b', 'level': 'warn', 'title': 'B'}, {'id': 'c', 'level': 'pass', 'title': 'C'}]
    new = [{'id': 'a', 'level': 'pass', 'title': 'A'}, {'id': 'b', 'level': 'warn', 'title': 'B'}, {'id': 'c', 'level': 'fail', 'title': 'C'}]
    fixed, added, still = lint.compare_reports(old, new)
    assert [c['id'] for c in fixed] == ['a'] and [c['id'] for c in added] == ['c'] and [c['id'] for c in still] == ['b']


# —— 报错定位到源码 ——

def test_where_points_to_source(tmp_path):
    v = make_video(tmp_path / 'v.mp4', f'if(eq(N,0),0,{MOVING})', audio=TONE)  # 第 0 帧黑屏
    sc = {'fps': FPS, 'durationFrames': 120, 'root': str(tmp_path),
          'sources': {'composition': {'file': 'src/Video.tsx', 'line': 3}},
          'shots': [{'name': 'hook', 'from': 0, 'to': 60, 'source': {'file': 'src/Hook.tsx', 'line': 12}, 'defined': {'file': 'src/timeline.ts', 'line': 7}},
                    {'name': 'body', 'from': 60, 'to': 120}],
          'hits': [], 'sfx': [], 'captions': []}
    r = lint_report(v, tmp_path / 'out', '--no-ocr', sidecar=sc)
    w = r['by']['open.frame0']['where'][0]
    assert (w['file'], w['line'], w['shot'], w['from'], w['to']) == ('src/Hook.tsx', 12, 'hook', 0, 0)
    assert w['timing'] == {'file': 'src/timeline.ts', 'line': 7}
    assert Path(w['path']) == tmp_path / 'src' / 'Hook.tsx'
    md = (tmp_path / 'out' / 'report.md').read_text(encoding='utf-8')
    assert 'src/Hook.tsx:12（镜头 hook，第 0 帧' in md and '时间在 src/timeline.ts:7' in md
    assert 'where' not in r['by']['meta']  # 只给 ✗ ⚠ 定位


def test_where_falls_back_to_composition():
    fps = 30
    checks = [{'id': 'black', 'level': 'warn', 'title': '', 'detail': '', 'data': [[70, 80]]},
              {'id': 'black', 'level': 'pass', 'title': '', 'detail': '', 'data': [[70, 80]]}]
    sc = {'sources': {'composition': {'file': 'src/Video.tsx', 'line': 3}}, 'shots': [{'name': 'body', 'from': 60, 'to': 120}]}
    lint.locate_checks(checks, sc, fps)
    assert checks[0]['where'] == [{'from': 70, 'to': 80, 'file': 'src/Video.tsx', 'line': 3, 'count': 1, 'shot': 'body'}]
    assert 'where' not in checks[1]
    c2 = [{'id': 'black', 'level': 'warn', 'title': '', 'detail': '', 'data': [[70, 80]]}]
    lint.locate_checks(c2, {'shots': [{'name': 'body', 'from': 60, 'to': 120}]}, fps)  # 旁注没有源码位置：不加 where
    assert 'where' not in c2[0]


# —— 标配：响度突增、静音空档、文字对比度、反转图案 ——

def _tone_video(tmp_path, name, amp):
    """3 秒 440 Hz，amp(t) 是每个采样的振幅"""
    sr = 48000
    t = np.arange(sr * 3) / sr
    y = amp(t) * np.sin(2 * math.pi * 440 * t)
    return _stereo_video(tmp_path, name, y, y)


def test_audio_jump(tmp_path):
    v = _tone_video(tmp_path, 'jump', lambda t: np.where(t < 1.5, 0.02, 0.6))  # 1.5 秒处 +30 dB
    r = lint_report(v, tmp_path / 'out', '--no-ocr', '--no-flash')
    assert r['by']['audio.jump']['level'] == 'warn'
    assert abs(r['by']['audio.jump']['data'][0]['from'] / FPS - 1.5) < 0.4
    # 同一处标成爆点：故意的，不算
    sc = {'fps': FPS, 'durationFrames': 90, 'shots': [], 'hits': [{'name': 'hit1', 'frame': 45, 'darkFrom': None, 'freeze': None}], 'sfx': [], 'captions': []}
    r = lint_report(v, tmp_path / 'out2', '--no-ocr', '--no-flash', sidecar=sc)
    assert r['by']['audio.jump']['level'] == 'pass'


def test_audio_silence_gap(tmp_path):
    v = _tone_video(tmp_path, 'gap', lambda t: np.where((t > 0.5) & (t < 1.8), 0.0, 0.3))
    r = lint_report(v, tmp_path / 'out', '--no-ocr', '--no-flash')
    assert r['by']['audio.silence']['level'] == 'warn', r['by']['audio.silence']['title']
    a, b = r['by']['audio.silence']['data'][0]
    assert 0.3 * FPS <= a <= 0.7 * FPS and 1.7 * FPS <= b <= 2.3 * FPS
    v = _tone_video(tmp_path, 'nogap', lambda t: 0.3 + 0 * t)
    assert lint_report(v, tmp_path / 'out2', '--no-ocr', '--no-flash')['by']['audio.silence']['level'] == 'pass'


def test_text_contrast_measure():
    bg = np.zeros((40, 200, 3), np.float32)
    white = bg.copy()
    white[15:25, 20:180:6] = 255  # 白字黑底
    assert lint.text_contrast(white) > 15
    dim = bg + 90
    dim[15:25, 20:180:6] = 115  # 灰字灰底
    assert lint.text_contrast(dim) < 2
    assert lint.text_contrast(bg + 50) is None  # 一片均匀：不判


@pytest.mark.parametrize('fg,expect', [(235, 'pass'), (62, 'warn')])
def test_text_contrast_check(tmp_path, fg, expect):
    # 画面中间一条「字」：竖条纹，前景 fg、背景 40
    lum = f"if(between(Y\\,60\\,100)*between(X\\,40\\,280)*lt(mod(X\\,8)\\,3)\\,{fg}\\,40)"
    v = make_video(tmp_path / 'c.mp4', lum, seconds=2)
    r = lint.Report()
    lint.contrast_check(r, v, {30: [(40 / 320, 60 / 180, 280 / 320, 100 / 180)]}, FPS, tmp_path, False)
    assert r.checks[0]['id'] == 'text.contrast' and r.checks[0]['level'] == expect, r.checks[0]['title']


def test_reversing_checkerboard_is_a_flash(tmp_path):
    # 棋盘格每 3 帧黑白互换一次（一秒 10 次）：WCAG 2.3.1 的「反转图案」，由闪烁检查抓到
    lum = "if(eq(mod(floor(X/40)+floor(Y/40)+floor(N/3)\\,2)\\,0)\\,230\\,20)"
    v = make_video(tmp_path / 'checker.mp4', lum, seconds=2)
    r = lint_report(v, tmp_path / 'out', '--no-ocr')
    assert r['by']['flash']['level'] == 'fail'


# —— 闭环：改法、fix.md、verify_fix ——

def test_fix_hints_and_fix_md(tmp_path):
    v = make_video(tmp_path / 'v.mp4', f'if(eq(N,0),0,{MOVING})', audio=TONE)
    r = lint_report(v, tmp_path / 'out', '--no-ocr')
    assert '淡入' in r['by']['open.frame0']['fix']
    assert 'fix' not in r['by']['meta']
    md = (tmp_path / 'out' / 'fix.md').read_text(encoding='utf-8')
    assert 'open.frame0' in md and '--compare' in md
    assert r['images']['fix'].endswith('fix.md')


def test_mcp_verify_fix(tmp_path):
    try:
        from video_lint import mcp_server
    except ImportError:
        import mcp_server
    old = make_video(tmp_path / 'v1.mp4', f'if(eq(N,0),0,{MOVING})', seconds=2, audio=TONE)
    first = mcp_server.lint_video(str(old), ocr=False)
    assert any(p['id'] == 'open.frame0' and p.get('fix') for p in first['problems'])
    assert first['fix_md'].endswith('fix.md')
    new = make_video(tmp_path / 'v2.mp4', MOVING, seconds=2, audio=TONE)
    res = mcp_server.verify_fix(str(new), first['report_json'], ocr=False)
    assert 'open.frame0' in res['fixed'] and res['added'] == [] and res['done'] is True
    # 输出目录和上一版一样会覆盖掉上一版：拒绝
    assert 'error' in mcp_server.verify_fix(str(new), first['report_json'], out=str(Path(first['report_json']).parent), ocr=False)
