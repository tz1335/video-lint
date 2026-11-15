"""旁注导入（SRT / EDL → sidecar）和 --font 缺字检查的测试。

  python -m pytest scripts/video-lint/tests -q
EDL 和字体都在测试里现场写出来；只有 test_build_from_video 要 ffmpeg / ffprobe。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
try:  # 独立仓库：代码在 video_lint 包里
    from video_lint import lint, sidecar_import as imp  # noqa: E402
except ImportError:  # 原工程：两个脚本就在上一级
    import lint  # noqa: E402
    import sidecar_import as imp  # noqa: E402

EDL = """TITLE: Demo Timeline
FCM: NON-DROP FRAME

001  AX       V     C        00:00:00:00 00:00:02:00 01:00:00:00 01:00:02:00
* FROM CLIP NAME: hook.mov

002  AX       A     C        00:00:00:00 00:00:05:00 01:00:00:00 01:00:05:00
* FROM CLIP NAME: music.wav

003  AX       V     C        00:00:10:00 00:00:10:00 01:00:02:00 01:00:02:00
003  B001     V     D    012 00:00:10:00 00:00:12:12 01:00:02:00 01:00:04:12
* FROM CLIP NAME: body.mov

004  BL       V     C        00:00:00:00 00:00:00:18 01:00:04:12 01:00:05:00
"""

SRT = """1
00:00:00,000 --> 00:00:01,500
第一句话

2
00:00:01,500 --> 00:00:03,000
你会怎么选？
"""


def test_timecode():
    assert imp.tc_frames('01:00:00:00', 30) == 108000
    assert imp.tc_frames('00:00:01:15', 30) == 45
    assert imp.tc_frames('00:00:01;15', 29.97) == 45  # 丢帧写法按名义帧率数
    assert imp.tc_frames('00:00:01.500', 30) == 45  # 毫秒


def test_parse_edl(tmp_path):
    p = tmp_path / 'cut.edl'
    p.write_text(EDL, encoding='utf-8')
    shots = imp.parse_edl(p, 30)
    # 音频事件跳过；转场的两条（003 C + 003 D）合成一条；时间线从 01:00:00:00 移到 0
    assert [(s['name'], s['from'], s['to']) for s in shots] == [('hook.mov', 0, 60), ('body.mov', 60, 132), ('shot003', 132, 150)]
    assert shots[1]['source'] == {'file': 'cut.edl', 'line': 3}


def test_build_without_video(tmp_path):
    (tmp_path / 'a.srt').write_text(SRT, encoding='utf-8')
    sc = imp.build(None, srt=tmp_path / 'a.srt', fps=30, platform='douyin', sid='demo')
    assert sc['format'] == 'video-lint-sidecar/0' and sc['id'] == 'demo' and sc['platform'] == 'douyin'
    assert [(c['from'], c['to'], c['text']) for c in sc['captions']] == [(0, 45, '第一句话'), (45, 90, '你会怎么选？')]
    with pytest.raises(SystemExit):
        imp.build(None, srt=tmp_path / 'a.srt')  # 没成片也没 --fps


@pytest.mark.skipif(not shutil.which('ffprobe'), reason='要 ffmpeg / ffprobe')
def test_build_from_video(tmp_path):
    v = tmp_path / 'v.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=160x284:r=25:d=4', '-pix_fmt', 'yuv420p', str(v)], check=True)
    (tmp_path / 'a.srt').write_text(SRT, encoding='utf-8')
    (tmp_path / 'cut.edl').write_text(EDL.replace('01:00:04:12 01:00:05:00', '01:00:04:12 01:00:09:00'), encoding='utf-8')
    out = tmp_path / 'sc.json'
    imp.main([str(v), '--srt', str(tmp_path / 'a.srt'), '--edl', str(tmp_path / 'cut.edl'), '-o', str(out)])
    sc = json.loads(out.read_text(encoding='utf-8'))
    assert (sc['fps'], sc['width'], sc['height'], sc['durationFrames']) == (25, 160, 284, 100)
    assert sc['captions'][1]['from'] == round(1.5 * 25)
    assert sc['shots'][-1]['to'] == 100 and sc['notes']  # EDL 比成片长：裁到成片并留一条说明


def test_cli_errors(tmp_path):
    with pytest.raises(SystemExit) as e:
        imp.main([str(tmp_path / 'nope.mp4'), '--srt', str(tmp_path / 'nope.srt')])
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        imp.main([str(tmp_path / 'nope.mp4')])  # 既没 --srt 也没 --edl


def make_font(path, chars):
    """只带 cmap 的最小字体：chars 里的字有，别的都没有"""
    fb = pytest.importorskip('fontTools.fontBuilder')
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    names = ['.notdef'] + [f'u{ord(c):04X}' for c in chars]
    b = fb.FontBuilder(1000, isTTF=True)
    b.setupGlyphOrder(names)
    b.setupCharacterMap({ord(c): f'u{ord(c):04X}' for c in chars})
    b.setupGlyf({n: TTGlyphPen(None).glyph() for n in names})
    b.setupHorizontalMetrics({n: (500, 0) for n in names})
    b.setupHorizontalHeader(ascent=800, descent=-200)
    b.setupNameTable({'familyName': 'Tiny', 'styleName': 'Regular'})
    b.setupOS2()
    b.setupPost()
    b.save(str(path))


def test_caption_glyphs(tmp_path):
    font = tmp_path / 'tiny.ttf'
    make_font(font, '第一句话你会怎么选')
    caps = [{'from': 0, 'to': 45, 'text': '第一句话 ok'}, {'from': 45, 'to': 90, 'text': '你会怎么选？★'}]
    g = lint.caption_glyphs(caps, [font], 'a.srt')
    assert g['status'] == 'fail'
    assert [(m['char'], m['where']) for m in g['missing']] == [('？', [{'file': 'a.srt', 'line': 2}]), ('★', [{'file': 'a.srt', 'line': 2}])]
    assert lint.caption_glyphs(caps[:1], [font])['status'] == 'pass'  # ASCII 和空格不查
    assert lint.caption_glyphs(caps, [tmp_path / 'missing.ttf'])['status'] == 'skip'
