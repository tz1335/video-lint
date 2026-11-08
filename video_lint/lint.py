"""video-lint：程序化视频的成片质检。只读成片和一份可选的旁注 JSON，不依赖任何 agent 或模型，规则全是确定性的。

用法：
  python -I scripts/video-lint/lint.py <成片.mp4> [--sidecar 旁注.json] [--out 输出目录]
         [--loud impact,press] [--lufs -15.5] [--strict]

产物都在 --out 目录（默认是成片旁边的 <文件名>-lint/）：
  report.md    给人看的报告
  report.json  给 agent 读的报告：每条检查是 {id, level: fail|warn|pass|info, title, detail, data}
  sheet-*.jpg  联系表（第 0 帧 + 每个镜头切走前一帧；竖屏叠了抖音界面遮挡区的近似框）
  hits.jpg     每个爆点前后几帧
  curve.png    整片亮度和运动曲线，标了镜头、爆点、硬切

旁注格式见同目录 README.md；这一工程里用 node scripts/video-lint/run.mjs <作品ID> 自动生成旁注并调用本脚本。
"""
import argparse
import base64
import html as _html
import fnmatch
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[2]
VERSION = '0.4.0'
# 「要听得见」的音效：冲击、动作音；whoosh、swish、paper、room 这类本来就轻，不查
LOUD_DEFAULT = ['impact', 'press', 'drop', 'chime', 'bell', 'clink', 'stamp', 'coin', 'tick', 'gunshot', 'crack', 'chapter', 'footstep', 'bloom']
ZONES_FILE = Path(__file__).with_name('zones.json')


def platform_zones(platform):
    """竖屏播放器界面的遮挡区和安全区（数据来自 reelsmith/safezone，见 zones.json），换算成 0–1 的 (名字, x0, y0, x1, y1)"""
    data = json.loads(ZONES_FILE.read_text(encoding='utf-8'))['platforms']
    p = data.get(platform)
    if not p:
        return None
    p = {**data[p['sameAs']], 'name': p['name']} if 'sameAs' in p else p
    rect = lambda z: (z['x'] / 100, z['y'] / 100, (z['x'] + z['w']) / 100, (z['y'] + z['h']) / 100)
    return {'name': p['name'], 'updated': p.get('updated'), 'zones': [(z['label'], *rect(z)) for z in p['zones']], 'safe': rect(p['safe'])}
# 一帧里 99.5% 分位亮度低于它，就算黑屏（0–255）。暗底风格的背景光晕本身就有 35–40，所以压到 24
BLACK_P995 = 24
# 「几乎不动」的帧差上限：h264 编码噪声在 0.01 上下；连线星图风缓慢漂移的粒子在 0.03–0.1，不能算静止
STATIC_DIFF = 0.025
# skip = 该查但查不了（没音轨、爆点不在片内、有硬切但没拍子网格）。整体状态：有 fail 是 fail，否则有 skip 是 unmeasured，否则 pass
ICON = {'fail': '✗', 'warn': '⚠', 'skip': '?', 'pass': '✓', 'info': '·'}
# --strict 时的退出码（参考 shorts-safe-zone 的三态，但避开 Python 崩溃时的 1）
EXIT = {'pass': 0, 'fail': 2, 'unmeasured': 3}


def run(args, text=False):
    return subprocess.run(args, capture_output=True, check=True, **({'text': True, 'encoding': 'utf-8', 'errors': 'replace'} if text else {}))


def font(size):
    for path in (ROOT / 'public/fonts/ZCOOLKuaiLe-Regular.ttf', 'C:/Windows/Fonts/msyh.ttc', '/System/Library/Fonts/PingFang.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    return ImageFont.load_default()


class Report:
    def __init__(self):
        self.checks = []

    def add(self, id, level, title, detail='', data=None):
        self.checks.append({'id': id, 'level': level, 'title': title, 'detail': detail, 'data': data})

    def count(self, level):
        return sum(c['level'] == level for c in self.checks)


def probe(video):
    j = json.loads(run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,width,height,r_frame_rate', '-show_entries', 'format=duration', '-of', 'json', str(video)], text=True).stdout)
    v = next(s for s in j['streams'] if s['codec_type'] == 'video')
    num, den = v['r_frame_rate'].split('/')
    return {
        'width': int(v['width']),
        'height': int(v['height']),
        'fps': float(num) / float(den),
        'duration': float(j['format']['duration']),
        'audio': any(s['codec_type'] == 'audio' for s in j['streams']),
    }


def frame_series(video, w, h):
    """整片解码成小灰度图，算每帧的平均亮度、99.5% 分位亮度、和上一帧的平均差"""
    sw = 160 if w >= h else 96
    sh = max(2, round(h * sw / w / 2) * 2)
    raw = run(['ffmpeg', '-v', 'error', '-i', str(video), '-vf', f'scale={sw}:{sh}:flags=area,format=gray', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-']).stdout
    g = np.frombuffer(raw, np.uint8).reshape(-1, sh * sw)
    mean = g.mean(axis=1)
    p995 = np.percentile(g, 99.5, axis=1)
    bright = (g > 60).mean(axis=1)
    diff = np.zeros(len(g))
    for i in range(1, len(g), 400):
        a = g[i - 1:i + 400].astype(np.int16)
        diff[i:i + len(a) - 1] = np.abs(a[1:] - a[:-1]).mean(axis=1)
    return mean, p995, diff, bright


def extract_frames(video, frames, tile_w, out_dir):
    """一次解码抽出若干帧（按帧号升序），缩到 tile_w 宽"""
    frames = sorted(set(frames))
    sel = '+'.join(f'eq(n,{f})' for f in frames)
    for old in out_dir.glob('_f*.png'):
        old.unlink()
    run(['ffmpeg', '-v', 'error', '-y', '-i', str(video), '-vf', f"select='{sel}',scale={tile_w}:-2", '-fps_mode', 'passthrough', str(out_dir / '_f%04d.png')])
    files = sorted(out_dir.glob('_f*.png'))
    ims = {f: Image.open(p).convert('RGB') for f, p in zip(frames, files)}
    for p in files:
        p.unlink()
    return ims


def magenta(rgb):
    """文字层里被改成 #ff00ff 的像素（半透明、淡入时会变暗，所以按色相判，不按亮度判）"""
    r, g, b = (rgb[..., i].astype(np.float32) for i in range(3))
    lo, hi = np.minimum(r, b), np.maximum(r, b)
    return (lo > 35) & (g < 0.5 * lo) & (hi - lo < 0.35 * hi)


def text_boxes(text_dir, video, out):
    """文字层 PNG 和成片同一帧对比：品红且原画面不是品红的像素就是文字；横向膨胀把一行字连成一块，返回每帧的行框（0–1 坐标）"""
    from scipy import ndimage

    meta = json.loads((Path(text_dir) / 'frames.json').read_text(encoding='utf-8'))
    frames = meta['frames']
    if not frames:
        return {}
    first = np.asarray(Image.open(Path(text_dir) / f'f{frames[0]}.png').convert('RGB'))
    H, W = first.shape[:2]
    normal = extract_frames(video, frames, W, out)
    boxes = {}
    for f in frames:
        lint = np.asarray(Image.open(Path(text_dir) / f'f{f}.png').convert('RGB'))
        base = np.asarray(normal[f].resize((W, H))) if f in normal else np.zeros_like(lint)
        mask = magenta(lint) & ~ndimage.binary_dilation(magenta(base), iterations=2)
        # 字间距在宋体大字里能到半个字宽，横向膨胀 6% 画面宽才能把一行连起来；纵向只膨胀几个像素，上下两行不粘
        lines = ndimage.binary_dilation(mask, structure=np.ones((5, max(3, int(W * 0.06))), bool))
        lab, _ = ndimage.label(lines)
        found = []
        for sl in ndimage.find_objects(lab):
            ys, xs = np.nonzero(mask[sl])
            if len(ys) < 25:
                continue
            y0, y1, x0, x1 = sl[0].start + ys.min(), sl[0].start + ys.max() + 1, sl[1].start + xs.min(), sl[1].start + xs.max() + 1
            # 低于画面高 0.8% 的细条多半是用 currentColor 画的分隔线、下划线（也被改成了品红），这么小的字手机上也读不出
            if (y1 - y0) / H < 0.008:
                continue
            found.append([round(x0 / W, 4), round(y0 / H, 4), round(x1 / W, 4), round(y1 / H, 4)])
        boxes[f] = merge_lines(found, W / H)
    return boxes


OCR_SHRINK = 0.74  # OCR 检测框比字形高一截（文字检测会把框往外扩），按中心收到约字高，和文字层的量法对齐（ep53 上 67 行对照：OCR 框高中位数是字高的 1.36 倍）


def ocr_engine():
    """有 RapidOCR 就返回识别器，没装返回 None（pip install rapidocr-onnxruntime）"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    return RapidOCR()


def ocr_boxes(engine, video, frames, out, max_w=960):
    """没有文字层时用 OCR 在这些帧上找文字行，返回和 text_boxes 一样的行框（0–1 坐标）。
    只认置信度 ≥ 0.5、含字母数字或汉字的行；画面里的图形偶尔会被当成字，结果只当兜底。"""
    P = probe(video)
    tw = min(P['width'], max_w)
    ims = extract_frames(video, frames, tw, out)
    boxes = {}
    for f, im in ims.items():
        W, H = im.size
        result, _ = engine(np.asarray(im)[:, :, ::-1].copy())
        lines = []
        for poly, text, score in result or []:
            if float(score) < 0.5 or not re.search(r'[0-9A-Za-z一-鿿]', text):
                continue
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            cy, hh = (min(ys) + max(ys)) / 2, (max(ys) - min(ys)) * OCR_SHRINK / 2
            lines.append([round(max(0, min(xs)) / W, 4), round(max(0, cy - hh) / H, 4), round(min(W, max(xs)) / W, 4), round(min(H, cy + hh) / H, 4)])
        boxes[f] = merge_lines(lines, W / H)
    return boxes


def merge_lines(found, aspect=1.0):
    """同一行的几块合成一个行框：纵向重叠超过较矮那块的一半，而且横向离得不远（空隙不超过两个字高）。
    字间距大的时候一行会被拆成几段，要拼回来；但同一高度、隔着半个画面的两个标签（ep53 圆环左右的「18 点」「6 点」）不能连成一条。
    aspect = 画面宽 / 高，用来把横向空隙换成字高的倍数。"""
    lines = []
    for b in sorted(found, key=lambda b: (b[0], b[1])):
        for L in lines:
            ov = min(L[3], b[3]) - max(L[1], b[1])
            hmin = min(L[3] - L[1], b[3] - b[1])
            gap = max(0.0, max(L[0], b[0]) - min(L[2], b[2])) * aspect  # 换成以画面高为单位
            if ov >= 0.5 * hmin and gap <= 2 * max(L[3] - L[1], b[3] - b[1]):
                L[:] = [min(L[0], b[0]), min(L[1], b[1]), max(L[2], b[2]), max(L[3], b[3])]
                break
        else:
            lines.append(list(b))
    return sorted(lines, key=lambda b: (b[1], b[0]))


def text_checks(r, boxes, zones, sc, fps, aspect, source='文字层（Remotion 渲的品红文字）'):
    """文字框进遮挡区、贴边出画、字高。aspect = 宽 / 高。
    至少盖住一个字（横向重叠 ≥ 0.8 个字高、纵向重叠 ≥ 半个字高）判 ✗；只擦到边（重叠占框面积 ≥ 10%）判 ⚠"""
    caps = sc.get('captions') or []
    said = lambda f: next((c['text'].split(' / ')[0][:14] for c in caps if c['from'] <= f < c['to']), '')
    hits, edges, heights = [], [], []
    for f, bs in sorted(boxes.items()):
        for b in bs:
            x0, y0, x1, y1 = b
            area = max((x1 - x0) * (y1 - y0), 1e-9)
            heights.append((y1 - y0, f))
            if x0 < 0.004 or y0 < 0.004 or x1 > 0.996 or y1 > 0.996:
                edges.append(f'第 {f} 帧（{f / fps:.2f} 秒）的文字框 {b} 贴到画面边上')
            lh = y1 - y0  # 字高（画面高的比例）
            for name, zx0, zy0, zx1, zy1 in (zones or {}).get('zones', []):
                ox, oy = max(0, min(x1, zx1) - max(x0, zx0)), max(0, min(y1, zy1) - max(y0, zy0))
                covered = bool(ox * aspect >= 0.8 * lh and oy >= 0.5 * lh)  # 横向重叠换成字高单位再比
                grazed = ox * aspect >= 0.3 * lh and oy >= 0.5 * lh  # 一行字的末端伸进去小半个字，也要提醒
                if covered or grazed or ox * oy / area >= 0.1:
                    hits.append((covered, float(ox * oy / area), f, name, b))
    if zones:
        bad = [h for h in hits if h[0]]
        lv = 'fail' if bad else 'warn' if hits else 'pass'
        # 按帧归并：一帧一行，列出被哪些区盖住、几行字
        by_frame = {}
        for c, k, f, name, b in hits:
            by_frame.setdefault(f, []).append((c, name, b))
        rows = []
        for f, hs in sorted(by_frame.items()):
            parts = [f'{sum(1 for c, n2, _ in hs if n2 == name and c)} 行盖住、{sum(1 for c, n2, _ in hs if n2 == name and not c)} 行擦边「{name}」'.replace('0 行盖住、', '').replace('、0 行擦边', '') for name in dict.fromkeys(n for _, n, _ in hs)]
            rows.append(f'  - {"✗" if any(c for c, _, _ in hs) else "⚠"} 第 {f} 帧（{f / fps:.2f} 秒）：' + '；'.join(parts) + (f'。字幕「{said(f)}」' if said(f) else ''))
        r.add('text.zones', lv, f'文字进{zones["name"]}遮挡区：{len({h[2] for h in bad})} 帧有字被盖住、{len(by_frame) - len({h[2] for h in bad})} 帧只擦边' if hits else f'文字都没进{zones["name"]}遮挡区',
              f'查了 {len(boxes)} 帧的{source}，联系表上青框是识别出的文字行；「盖住」= 至少一个字在区里。' + ('\n' + '\n'.join(rows) if rows else ''),
              [{'frame': f, 'zone': name, 'covered': c, 'overlap': round(k, 3), 'box': b} for c, k, f, name, b in hits])
    r.add('text.edge', 'warn' if edges else 'pass', f'有 {len(edges)} 行文字贴边，可能出画' if edges else '文字都没贴边', '\n'.join(f'  - {e}' for e in edges[:20]))
    if heights:
        early = [h for h, f in heights if f < 2 * fps]
        small = sorted(heights)[:3]
        r.add('text.size', 'info', f'字高（行框高 ÷ 画面高）：最大 {max(h for h, _ in heights):.1%}，最小 {small[0][0]:.1%}' + (f'；前 2 秒最大 {max(early):.1%}' if early else '；前 2 秒没有文字'),
              '最小的几行：' + '、'.join(f'第 {f} 帧 {h:.1%}' for h, f in small), {'max': max(h for h, _ in heights), 'firstTwoSecMax': max(early) if early else 0})


# 阿拉伯数字；或者中文数字带量词。单独的「一」不算（「一个人」「一句话」不是数字），「一百」「一千」才算
NUM_RE = re.compile(r'[0-9０-９]|(?:[两三四五六七八九十]|一[百千万亿])[一二两三四五六七八九十百千万亿]*\s*(?:年|岁|人|个|倍|成|万|亿|%|分钟|小时|天|次|份|位)')
# 规则出处：在原工程里指向那份复盘笔记；单独发布时笔记不在，就只写规则名
_RULES_DOC = 'materials/00-公共/选题脚本/00-爆款结构-抖腿.md'
RULES = f'{_RULES_DOC}「前 2 秒」' if (ROOT / _RULES_DOC).exists() else '「前 2 秒」规则'


def opening_rules(r, sc, boxes, bright, fps, n, P, from_ocr=False):
    """「前 2 秒」硬规则里能机器判断的几条（来自 ep52 的教训，见 RULES）。字的大小按「字高 ÷ 视频宽」算：
    手机上横竖屏的视频都是撑满屏宽，这样两种画幅能放在一起比。"""
    W, H = P['width'], P['height']
    portrait = H > W
    rows = []  # (规则号, 级别, 标题, 说明)

    # 1. 第 0 帧就是完整画面：主体不能从第 0 帧才开始长（第 0 帧黑屏 / 空画面已经由 open.frame0 判过，这里不重复）
    f0_level = next((c['level'] for c in r.checks if c['id'] == 'open.frame0'), 'pass')
    later = float(np.median(bright[int(0.5 * fps):int(2 * fps)])) if n > 2 * fps else float(bright.mean())
    if f0_level not in ('fail', 'warn'):
        if later > 0 and bright[0] < 0.5 * later:
            rows.append((1, 'warn', '第 0 帧画面还没长全', f'亮像素只有 0.5–2 秒时的 {bright[0] / later:.0%}；主体要在第 0 帧就画好，「长出来」留给背景和后面的元素'))
        else:
            rows.append((1, 'pass', '第 0 帧画面已经完整', f'亮像素是 0.5–2 秒时的 {bright[0] / max(later, 1e-6):.0%}'))

    # 2. 第一句在第 0 帧就整句在屏，字要大到 0.5 秒读得完
    if boxes:
        size = lambda b: (b[3] - b[1]) * H / W  # 字高 ÷ 视频宽
        b0 = boxes.get(0, [])
        b1 = boxes.get(next((f for f in sorted(boxes) if f >= fps), -1), [])
        if not b0:
            first = next((f for f in sorted(boxes) if boxes[f]), None)
            # 文字框来自 OCR 时只判 ⚠：OCR 可能漏掉小字、艺术字、低对比的字
            rows.append((2, 'warn' if from_ocr else 'fail', '第 0 帧没有字' + ('（OCR 没找到）' if from_ocr else ''), f'第一行字出现在第 {first} 帧（{first / fps:.2f} 秒）' if first is not None else '前 2 秒都没有字'))
        else:
            big = max(b0, key=size)
            same = [b for b in b1 if min(b[3], big[3]) - max(b[1], big[1]) > 0.5 * (big[3] - big[1])]
            grow = (big[2] - big[0]) / max(b[2] - b[0] for b in same) if same else 1.0
            lv = 'fail' if size(big) < 0.025 else 'warn' if size(big) < 0.04 or grow < 0.8 else 'pass'
            rows.append((2, lv, f'第 0 帧最大的字：字高是视频宽的 {size(big):.1%}' + ('，这一行到 1 秒还在变长（逐字出现）' if grow < 0.8 else ''),
                         '标准：≥ 4% 能一眼读（1080 宽约 43 像素，手机上约 16 号字）；< 2.5% 基本读不出。ep54 的第 0 帧是一堆手机聊天小字，2 秒跳出 51.5%'))
    else:
        rows.append((2, 'info', '没有文字层，第 0 帧的字没法量', '传 --text-dir（所有文字渲成纯品红的同帧 PNG）才能量；原工程里 run.mjs 会自动渲'))
    caps = sc.get('captions') or []
    first_cap = caps[0] if caps else None
    if first_cap and first_cap['from'] <= fps:
        zh = first_cap['text'].split(' / ')[0]
        plain = re.sub(r'[〔〕〖〗【】\s，。？！、,.?!—…「」]', '', zh)
        rows.append((2, 'warn' if len(plain) > 10 else 'pass', f'第一句「{zh}」{len(plain)} 个字' + ('，超过 10 个' if len(plain) > 10 else ''), '标准：≤ 10 个字，0.5 秒读得完'))

    # 4. 竖屏冷开场：上半屏第 0 帧起常驻一句钩子（还要躲开顶栏，那由 text.zones 判）
    if portrait and boxes:
        early = [f for f in sorted(boxes) if f < 2 * fps]
        top = [f for f in early if any((b[1] + b[3]) / 2 < 0.45 for b in boxes[f])]
        lv = 'pass' if len(top) == len(early) else 'warn'
        rows.append((4, lv, '上半屏有常驻钩子' if lv == 'pass' else f'前 2 秒上半屏只有 {len(top)} / {len(early)} 帧有字', '标准：竖屏冷开场第 0 帧起，上半屏挂一句第二人称的问题，挂到标题卡前（放在顶栏 8% 以下）'))

    # 5. 8 秒内出一个带人数或年份的数字
    if caps:
        hit = next((c for c in caps if c['from'] < 8 * fps and NUM_RE.search(c['text'].split(' / ')[0])), None)
        rows.append((5, 'pass' if hit else 'warn', f'8 秒内出了数字：「{hit["text"].split(" / ")[0]}」（{hit["from"] / fps:.1f} 秒）' if hit else '8 秒内的字幕里没有数字', '标准：8 秒内出一个带人数或年份的数字'))
    else:
        rows.append((5, 'info', '没有字幕表，8 秒内有没有数字没法查', '画面里的数字（比如人生曲线的年龄）读不到'))

    for k, lv, title, detail in rows:
        r.add(f'open.rule{k}', lv, f'开头规则 {k}：{title}', f'{detail}（{RULES}第 {k} 条）')
    r.add('open.manual', 'info', '开头规则 3、6 要人看', f'3：第一句是断言或悬念、带利害，不是场景；6：单独看 frame0.jpg，在信息流里能不能看懂这期讲什么（{RULES}）')
    return {'frame0MaxText': round(max(((b[3] - b[1]) * H / W for b in boxes.get(0, [])), default=0), 4) if boxes else None,
            'openRuleFails': sum(1 for _, lv, _, _ in rows if lv == 'fail'), 'openRuleWarns': sum(1 for _, lv, _, _ in rows if lv == 'warn')}


def label(im, text, zones=None, boxes=None):
    """zones：platform_zones() 的结果；红框是界面遮挡区，绿色虚线框是推荐安全区；boxes：文字行框，画青色框"""
    im = im.copy()
    d = ImageDraw.Draw(im, 'RGBA')
    w, h = im.size
    for x0, y0, x1, y1 in boxes or []:
        d.rectangle([x0 * w - 1, y0 * h - 1, x1 * w + 1, y1 * h + 1], outline=(60, 230, 255, 230), width=2)
    if zones:
        for name, x0, y0, x1, y1 in zones['zones']:
            d.rectangle([x0 * w, y0 * h, x1 * w - 1, y1 * h - 1], fill=(255, 40, 40, 38), outline=(255, 60, 60, 170), width=1)
        x0, y0, x1, y1 = zones['safe']
        for i in range(int(x0 * w), int(x1 * w), 8):
            d.line([i, y0 * h, min(i + 4, x1 * w), y0 * h], fill=(90, 230, 120, 200))
            d.line([i, y1 * h, min(i + 4, x1 * w), y1 * h], fill=(90, 230, 120, 200))
        for j in range(int(y0 * h), int(y1 * h), 8):
            d.line([x0 * w, j, x0 * w, min(j + 4, y1 * h)], fill=(90, 230, 120, 200))
            d.line([x1 * w, j, x1 * w, min(j + 4, y1 * h)], fill=(90, 230, 120, 200))
    f = font(max(12, w // 22))
    tw = d.textlength(text, font=f)
    d.rectangle([0, 0, tw + 12, f.size + 10], fill=(0, 0, 0, 190))
    d.text((6, 4), text, fill=(255, 220, 90), font=f)
    return im


def grid(tiles, cols, out):
    w, h = tiles[0].size
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new('RGB', (w * min(cols, len(tiles)), h * rows), (12, 12, 12))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * w, (i // cols) * h))
    sheet.save(out, quality=88)


def runs(mask, min_len):
    """mask 里连续为 True、长度 ≥ min_len 的段 [起, 止)"""
    out, start = [], None
    for i, m in enumerate(list(mask) + [False]):
        if m and start is None:
            start = i
        elif not m and start is not None:
            if i - start >= min_len:
                out.append((start, i))
            start = None
    return out


def hard_cuts(diff):
    """单帧尖峰才算硬切；叠化会摊到好几帧上，不算"""
    cuts = []
    for n in range(1, len(diff)):
        lo, hi = max(1, n - 6), min(len(diff), n + 7)
        around = np.concatenate([diff[lo:max(lo, n - 1)], diff[min(hi, n + 2):hi]])
        base = float(np.median(around)) if len(around) else 0.0
        prev = diff[n - 1] if n > 1 else 0.0
        nxt = diff[n + 1] if n + 1 < len(diff) else 0.0
        if diff[n] > 18 and diff[n] > 3 * base + 2 and diff[n] > 1.8 * max(prev, nxt):
            cuts.append(n)
    return cuts


def shot_at(shots, f):
    if not shots:
        return '—'
    return next((s['name'] for s in shots if s['from'] <= f < s['to']), '片尾' if f >= shots[-1]['to'] else '—')


# —— 闪烁安全（WCAG 2.3.1 三次闪烁阈值）——
# 一次「闪烁」= 一对方向相反的亮度变化：相对亮度变化 ≥ 10%，且较暗的一边低于 0.80。
# 一秒内超过 3 次闪烁、闪烁面积又足够大，就可能诱发光敏性癫痫。面积阈值取 WCAG 的近似：
# 1024×768 屏幕上 341×256 像素，约占画面 11%。这里把整帧里同方向变化的像素加起来算面积（WCAG 说的是任意 10° 视野内），
# 会比标准略严，宁可多报。红色闪烁：其中一边是饱和红（R/(R+G+B) ≥ 0.8），且 (R−G−B)×320 变化 > 20。
FLASH_AREA = 341 * 256 / (1024 * 768)


def _linear(c):
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def flash_events(video, w, h, fps):
    """逐帧找「大面积同方向的亮度跳变」和「红色跳变」，返回 [(帧, +1/-1)] 两组。每个像素用滞回跟踪：
    从上一个极值变化够了才算一次跳变，慢慢淡入淡出不会被拆成很多次。"""
    sw = 96 if w >= h else 54
    sh = max(2, round(h * sw / w / 2) * 2)
    raw = run(['ffmpeg', '-v', 'error', '-i', str(video), '-vf', f'scale={sw}:{sh}:flags=area,format=rgb24', '-fps_mode', 'passthrough', '-f', 'rawvideo', '-']).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, sh * sw, 3).astype(np.float32)
    lum_ext = red_ext = red_sat = lum_dir = red_dir = None
    lum_ev, red_ev = [], []
    for t, px in enumerate(frames):
        L = 0.2126 * _linear(px[:, 0]) + 0.7152 * _linear(px[:, 1]) + 0.0722 * _linear(px[:, 2])
        rgb = px / 255.0
        total = rgb.sum(axis=1) + 1e-6
        sat = rgb[:, 0] / total >= 0.8
        red = np.clip((rgb[:, 0] - rgb[:, 1] - rgb[:, 2]) * 320, 0, None)
        if lum_ext is None:
            lum_ext, lum_dir = L.copy(), np.zeros_like(L)
            red_ext, red_sat, red_dir = red.copy(), sat.copy(), np.zeros_like(red)
            continue
        # 亮度：离上一个极值 ≥ 0.1，且较暗的一边 < 0.8
        d = L - lum_ext
        dark_ok = np.minimum(L, lum_ext) < 0.8
        up = (d >= 0.1) & dark_ok & (lum_dir <= 0)
        down = (d <= -0.1) & dark_ok & (lum_dir >= 0)
        cont = ((lum_dir > 0) & (L > lum_ext)) | ((lum_dir < 0) & (L < lum_ext))
        lum_ext = np.where(up | down | cont, L, lum_ext)
        lum_dir = np.where(up, 1, np.where(down, -1, lum_dir))
        if up.mean() >= FLASH_AREA:
            lum_ev.append((t, 1))
        if down.mean() >= FLASH_AREA:
            lum_ev.append((t, -1))
        # 红色：(R−G−B)×320 离上一个极值 > 20，且两边至少一边是饱和红
        d = red - red_ext
        ok = sat | red_sat
        up = (d > 20) & ok & (red_dir <= 0)
        down = (d < -20) & ok & (red_dir >= 0)
        cont = ((red_dir > 0) & (red > red_ext)) | ((red_dir < 0) & (red < red_ext))
        moved = up | down | cont
        red_ext = np.where(moved, red, red_ext)
        red_sat = np.where(moved, sat, red_sat)
        red_dir = np.where(up, 1, np.where(down, -1, red_dir))
        if up.mean() >= FLASH_AREA:
            red_ev.append((t, 1))
        if down.mean() >= FLASH_AREA:
            red_ev.append((t, -1))
    return lum_ev, red_ev


def flash_windows(events, fps):
    """任意 1 秒窗口里的跳变数；超过 6 次（= 超过 3 次闪烁）的窗口合并成时间段。返回 (最多的跳变数, [(起帧, 止帧, 次数)])"""
    ts = [t for t, _ in events]
    worst, bad = 0, []
    for i, t0 in enumerate(ts):
        k = sum(1 for t in ts[i:] if t < t0 + fps)
        worst = max(worst, k)
        if k > 6:
            end = max(t for t in ts[i:] if t < t0 + fps)
            if bad and t0 <= bad[-1][1]:
                bad[-1] = (bad[-1][0], max(bad[-1][1], end), max(bad[-1][2], k))
            else:
                bad.append((t0, end, k))
    return worst, bad


def flash_check(r, video, P, fps, shots):
    lum_ev, red_ev = flash_events(video, P['width'], P['height'], fps)
    for key, name, ev in (('flash', '闪烁', lum_ev), ('flash.red', '红色闪烁', red_ev)):
        worst, bad = flash_windows(ev, fps)
        if bad:
            rows = '\n'.join(f'  - {a / fps:.2f}–{b / fps:.2f} 秒（{shot_at(shots, a)}）：1 秒内 {k} 次大面积{name[:-2] or ""}跳变 ≈ {k / 2:.1f} 次闪烁' for a, b, k in bad)
            r.add(key, 'fail', f'{name}超过每秒 3 次：{len(bad)} 段', '标准 WCAG 2.3.1：任意 1 秒内不超过 3 次闪烁（面积按整帧约 11% 算，比标准略严）。可能诱发光敏性癫痫，把闪白 / 硬切拉开到 1 秒 3 次以内，或者缩小闪烁面积。\n' + rows,
                  [{'from': a, 'to': b, 'transitions': k} for a, b, k in bad])
        else:
            r.add(key, 'pass', f'没有超标的{name}', f'任意 1 秒内最多 {worst} 次大面积跳变（{worst / 2:.1f} 次闪烁），标准是不超过 3 次闪烁', {'maxTransitionsPerSecond': worst})


# —— 上传规格（specs.json，只提醒不判 ✗）——
SPECS_FILE = Path(__file__).with_name('specs.json')


def _ratio(w, h):
    from math import gcd
    g = gcd(w, h)
    return f'{w // g}:{h // g}'


def spec_check(r, video, platform):
    """编码、像素格式、帧率、分辨率、画幅、码率、时长、音频，对照目标平台的推荐值"""
    spec = json.loads(SPECS_FILE.read_text(encoding='utf-8'))
    c, p = spec['common'], spec['platforms'].get(platform) or spec['platforms']['none']
    j = json.loads(run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels', '-show_entries', 'format=duration,bit_rate,size', '-of', 'json', str(video)], text=True).stdout)
    v = next(x for x in j['streams'] if x['codec_type'] == 'video')
    au = next((x for x in j['streams'] if x['codec_type'] == 'audio'), None)
    fmt = j.get('format', {})
    w, h = int(v['width']), int(v['height'])
    fr = lambda s_: (lambda a, b: float(a) / float(b) if float(b) else 0.0)(*s_.split('/'))
    fps, avg = fr(v.get('r_frame_rate', '0/1')), fr(v.get('avg_frame_rate', '0/1'))
    dur, rate = float(fmt.get('duration') or 0), float(fmt.get('bit_rate') or 0)
    issues = []
    if v.get('codec_name') not in c['videoCodecs']:
        issues.append(f'视频编码 {v.get("codec_name")}，平台一般要 {" / ".join(c["videoCodecs"])}，上传后会被重新压缩或者传不上去')
    if v.get('pix_fmt') not in c['pixelFormats']:
        issues.append(f'像素格式 {v.get("pix_fmt")}，手机和平台普遍只认 yuv420p（导出时加 -pix_fmt yuv420p）')
    if fps > c['maxFps'] + 0.01:
        issues.append(f'帧率 {fps:g} fps，超过 {c["maxFps"]}')
    if avg and abs(avg - fps) > 0.5:
        issues.append(f'可变帧率（标称 {fps:g}、平均 {avg:.2f} fps），平台转码时可能音画不同步')
    if w % 2 or h % 2:
        issues.append(f'分辨率 {w}×{h} 有奇数边，部分编码器和播放器不支持')
    if min(w, h) < p['minShortSide']:
        issues.append(f'短边只有 {min(w, h)} 像素，低于 {p["minShortSide"]}，在手机上会糊' + (f'（推荐 {p["recommended"]}）' if p['recommended'] else ''))
    if p['aspects'] and _ratio(w, h) not in p['aspects']:
        issues.append(f'画幅 {_ratio(w, h)}，{p["name"]}信息流主要是 {" / ".join(p["aspects"])}，会加黑边或被裁')
    hints = []
    if rate and min(w, h) >= 1080 and rate < c['minBitrate1080p']:
        # 只提示不报警：按质量（CRF）导出时，暗底、静止多的片子码率本来就低，画质并不差（ep52 1.3 Mbps、ep53 1.8 Mbps 都是这样）
        hints.append(f'码率 {rate / 1e6:.1f} Mbps，低于 1080p 常见的 {c["minBitrate1080p"] / 1e6:.1f} Mbps：画面细节多的话平台再压一次可能偏糊；暗底、静止多的片子码率本来就低，可以不管')
    if p['maxSeconds'] and dur > p['maxSeconds']:
        issues.append(f'时长 {dur:.1f} 秒，超过{p["name"]}的 {p["maxSeconds"]} 秒上限')
    if au:
        if au.get('codec_name') not in c['audioCodecs']:
            issues.append(f'音频编码 {au.get("codec_name")}，一般用 AAC')
        if au.get('sample_rate') and int(au['sample_rate']) not in c['audioSampleRates']:
            issues.append(f'音频采样率 {au["sample_rate"]} Hz，一般是 44100 或 48000')
    summary = f'{w}×{h}，{fps:g} fps，{v.get("codec_name")} / {v.get("pix_fmt")}，{rate / 1e6:.1f} Mbps，{dur:.1f} 秒' + (f'，音频 {au.get("codec_name")} {au.get("sample_rate")} Hz' if au else '')
    r.add('spec', 'warn' if issues else 'pass', f'上传规格（{p["name"]}）：' + (f'{len(issues)} 处要注意' if issues else '都在推荐范围内'),
          summary + '。推荐值来自 specs.json（' + spec['checked'] + ' 核对），平台会改，以官方为准。' + ('\n' + '\n'.join(f'  - {x}' for x in issues) if issues else '') + ''.join(f'\n  - （提示）{x}' for x in hints), {'issues': issues, 'hints': hints, 'width': w, 'height': h, 'fps': fps, 'bitrate': rate, 'duration': dur})


# —— 字幕文件导入和阅读速度 ——
_TS = re.compile(r'(\d+):(\d{2}):(\d{2})[,.](\d{1,3})')


def _sec(m):
    h, mi, se, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(frac.ljust(3, '0')[:3]) / 1000


def parse_captions(path, fps):
    """SRT / VTT / ASS → [{'from': 帧, 'to': 帧, 'text': 文本}]，去掉样式标签"""
    text = Path(path).read_text(encoding='utf-8-sig', errors='replace')
    out = []
    if Path(path).suffix.lower() in ('.ass', '.ssa'):
        fields = None
        for line in text.splitlines():
            if line.startswith('Format:') and fields is None and 'Start' in line:
                fields = [f.strip() for f in line[7:].split(',')]
            elif line.startswith('Dialogue:') and fields:
                parts = line[9:].split(',', len(fields) - 1)
                row = dict(zip(fields, parts))
                t0, t1 = (_TS.match('0' + row['Start'].strip() if row['Start'].strip().count(':') == 2 and len(row['Start'].strip().split(':')[0]) == 1 else row['Start'].strip()),
                          _TS.match('0' + row['End'].strip() if row['End'].strip().count(':') == 2 and len(row['End'].strip().split(':')[0]) == 1 else row['End'].strip()))
                body = re.sub(r'\{[^}]*\}', '', row.get('Text', '')).replace(chr(92) + 'N', ' ').replace(chr(92) + 'n', ' ').strip()  # ASS 的换行写成反斜杠 N / n
                if t0 and t1 and body:
                    out.append({'from': round(_sec(t0) * fps), 'to': round(_sec(t1) * fps), 'text': body})
    else:  # SRT / VTT：一块一块，时间行是「起 --> 止」
        for block in re.split(r'\n\s*\n', text.replace('\r\n', '\n')):
            lines = [l for l in block.strip().split('\n') if l.strip()]
            for i, l in enumerate(lines):
                if '-->' in l:
                    a, b = l.split('-->')[:2]
                    m0, m1 = _TS.search(a if a.count(':') == 2 else '00:' + a.strip()), _TS.search(b if b.split()[0].count(':') == 2 else '00:' + b.strip())
                    body = re.sub(r'<[^>]+>', '', ' '.join(lines[i + 1:])).strip()
                    if m0 and m1 and body:
                        out.append({'from': round(_sec(m0) * fps), 'to': round(_sec(m1) * fps), 'text': body})
                    break
    return sorted(out, key=lambda c: c['from'])


CJK = re.compile(r'[㐀-鿿豈-﫿぀-ヿ가-힯]')


def reading_speed_check(r, caps, fps):
    """每条字幕每秒要读多少字：中日韩文字超过 9 字 / 秒、其它文字超过 20 字符 / 秒 ⚠；停留不到 5/6 秒 ⚠（Netflix 字幕规范）。
    旁注里「主句 / 副句」这种双语或两层字幕只算主句。"""
    fast, short = [], []
    for c in caps:
        main = c['text'].split(' / ')[0]
        sec = max((c['to'] - c['from']) / fps, 1e-3)
        cjk = len(CJK.findall(main))
        if cjk >= len(re.findall(r'[A-Za-z0-9]', main)):
            n, limit, unit = cjk + len(re.findall(r'[A-Za-z0-9]+', main)), 9, '字'
        else:
            n, limit, unit = len(re.sub(r'\s+', ' ', main).strip()), 20, '字符'
        if n / sec > limit:
            fast.append(f'  - {c["from"] / fps:.2f} 秒「{main[:24]}」{n} {unit} / {sec:.2f} 秒 = {n / sec:.1f} {unit}/秒（上限 {limit}）')
        if sec < 5 / 6:
            short.append(f'  - {c["from"] / fps:.2f} 秒「{main[:24]}」只停留 {sec:.2f} 秒')
    lv = 'warn' if fast or short else 'pass'
    r.add('captions.speed', lv, f'字幕阅读速度：{len(caps)} 条里 {len(fast)} 条太快、{len(short)} 条停留太短' if lv == 'warn' else f'字幕阅读速度：{len(caps)} 条都读得完',
          '标准（Netflix 字幕规范）：中文每秒不超过 9 个字，英文每秒不超过 20 个字符，每条至少停留 5/6 秒。' + ('\n' + '\n'.join(fast[:20] + short[:10]) if lv == 'warn' else ''), {'fast': len(fast), 'short': len(short)})


def audio_mono(video, sr=48000):
    raw = run(['ffmpeg', '-v', 'error', '-i', str(video), '-vn', '-ac', '1', '-ar', str(sr), '-f', 'f32le', '-']).stdout
    return np.frombuffer(raw, np.float32), sr


def loudness(video):
    err = subprocess.run(['ffmpeg', '-nostats', '-i', str(video), '-vn', '-af', 'ebur128=peak=true', '-f', 'null', '-'], capture_output=True, text=True, encoding='utf-8', errors='replace').stderr
    summary = err.split('Summary:')[-1]
    num = lambda pat: float(m.group(1)) if (m := re.search(pat, summary)) else float('nan')
    return {'I': num(r'I:\s+(-?[\d.]+) LUFS'), 'LRA': num(r'LRA:\s+(-?[\d.]+) LU'), 'TP': num(r'Peak:\s+(-?[\d.]+|-inf) dBFS')}


def db(x):
    return 10 * math.log10(float(np.mean(x.astype(np.float64) ** 2)) + 1e-12) if len(x) else float('nan')


def band_db(x, sr, lo=1200, hi=5000):
    if not len(x):
        return float('nan')
    X = np.fft.rfft(x * np.hanning(len(x)))
    f = np.fft.rfftfreq(len(x), 1 / sr)
    return 10 * math.log10(float(np.sum(np.abs(X[(f >= lo) & (f <= hi)]) ** 2)) / len(x) + 1e-12)


def sfx_stem_check(r, sc, y, sr, stem, fps, loud, out_dir=None):
    """用「只有音效」的分轨判断每个音效在成片里压不压得过配乐（音效 / 配乐能量比，SMR）。
    成片 = g × 音效分轨 + 配乐（+ 人声）：g 用整条音轨的最小二乘估（分轨可能单独做过响度标准化，成片可能限过幅），
    配乐 = 成片 − g × 音效分轨。每个音效取它开始后 0.2 秒，全频和 1.2–5 kHz 各算一次。"""
    s, _ = audio_mono(stem)
    # 两次导出的 AAC 编码起点可能差几毫秒（ep60 差 5 毫秒），先在 ±0.1 秒里找互相关最大的偏移对齐
    k = min(len(y), len(s), 30 * sr)
    cc = np.fft.irfft(np.fft.rfft(y[:k], 2 * k) * np.conj(np.fft.rfft(s[:k], 2 * k)))
    L = int(0.1 * sr)
    lags = np.concatenate([np.arange(L + 1), np.arange(-L, 0)])
    lag = int(lags[np.argmax(np.concatenate([cc[:L + 1], cc[-L:]]))])
    s = np.concatenate([np.zeros(lag, np.float32), s]) if lag > 0 else s[-lag:]
    m = min(len(y), len(s))
    yy, ss = y[:m].astype(np.float64), s[:m].astype(np.float64)
    g = float(np.dot(yy, ss) / max(np.dot(ss, ss), 1e-12))
    if g <= 0:
        r.add('audio.sfx', 'skip', '音效分轨和成片对不上，没法用分轨判断', f'估出的增益 g = {g:.3f}（应当是正数）：分轨可能不是同一版导出的')
        return
    music = yy - g * ss
    rows = []
    for e in sc.get('sfx', []):
        if loud and not any(k in e['name'] for k in loud):
            continue
        i = int(e['frame'] / fps * sr)
        w = slice(i, min(m, i + int(0.2 * sr)))
        if w.stop - i < int(0.05 * sr) or np.dot(ss[w], ss[w]) < 1e-10:
            continue
        smr = db(g * ss[w]) - db(music[w])
        smr_hi = band_db(g * ss[w], sr) - band_db(music[w], sr)
        rows.append({'name': e['name'], 'frame': e['frame'], 'smr': round(smr, 1), 'smrHi': round(smr_hi, 1)})
    if not rows:
        r.add('audio.sfx', 'info', '音效分轨里旁注的音效位置都没有声音', '')
        return
    # 判据：全频和 1.2–5 kHz 都比配乐低 10 dB 以上算几乎听不见；哪一边高过配乐 −3 dB 就算听得清
    lost = [x for x in rows if x['smr'] < -10 and x['smrHi'] < -10]
    clear = [x for x in rows if x['smr'] >= -3 or x['smrHi'] >= -3]
    lv = 'warn' if lost else 'pass'
    if lost and out_dir:
        # 把「几乎听不见」的几处各剪 1.5 秒（音效前 0.5 秒到后 1 秒）拼起来，中间隔 0.4 秒静音，听一遍就能校准阈值
        import wave
        gap = np.zeros(int(0.4 * sr))
        clips = np.concatenate([np.concatenate([yy[max(0, int((x['frame'] / fps - 0.5) * sr)):int((x['frame'] / fps + 1.0) * sr)], gap]) for x in lost[:12]])
        with wave.open(str(Path(out_dir) / 'sfx-lost.wav'), 'wb') as wv:
            wv.setnchannels(1)
            wv.setsampwidth(2)
            wv.setframerate(sr)
            wv.writeframes((np.clip(clips, -1, 1) * 32767).astype(np.int16).tobytes())
    fmt = lambda x: f'  - {x["name"]} @ {x["frame"] / fps:.2f} 秒：全频 {x["smr"]:+.1f} dB，1.2–5 kHz {x["smrHi"]:+.1f} dB'
    r.add('audio.sfx', lv, f'音效压不压得过配乐（用分轨算）：{len(clear)} / {len(rows)} 处听得清，{len(lost)} 处几乎听不见',
          f'分轨 {Path(stem).name}，对齐偏移 {lag / sr * 1000:+.1f} 毫秒，成片里音效的增益 g = {g:.3f}。数字是音效比配乐高多少 dB：全频和 1.2–5 kHz 都低于 −10 dB 算几乎听不见，有一边 ≥ −3 dB 算听得清'
          + ('\n几乎听不见的（加音量，或者给它让出频段）：\n' + '\n'.join(fmt(x) for x in lost)
             + (f'\n前 {min(12, len(lost))} 处剪成了 {Path(out_dir) / "sfx-lost.wav"}（每段从音效前 0.5 秒到后 1 秒），听一遍确认阈值准不准' if out_dir else '') if lost else '')
          + '\n全部：\n' + '\n'.join(fmt(x) for x in rows[:60]), {'gain': g, 'events': rows})


def draw_curve(mean, diff, fps, sc, cuts, out):
    W, H, pad = 1600, 300, 30
    n = len(mean)
    im = Image.new('RGB', (W, H), (16, 16, 18))
    d = ImageDraw.Draw(im)
    f = font(14)
    x = lambda i: pad + (W - 2 * pad) * i / max(1, n - 1)
    d.rectangle([x(0), pad, x(min(n - 1, 2 * fps)), H - pad], fill=(40, 34, 20))  # 前 2 秒
    for s in sc.get('shots', []):
        d.line([x(s['from']), pad, x(s['from']), H - pad], fill=(70, 70, 80))
    for c in cuts:
        d.line([x(c), pad - 8, x(c), pad], fill=(90, 160, 255), width=2)
    for h in sc.get('hits', []):
        d.line([x(h['frame']), pad, x(h['frame']), H - pad], fill=(255, 80, 80), width=2)
    top = max(1.0, float(mean.max()))
    d.line([(x(i), H - pad - (H - 2 * pad) * mean[i] / top) for i in range(n)], fill=(255, 210, 90), width=2)
    d.line([(x(i), H - pad - (H - 2 * pad) * min(diff[i], 20) / 20) for i in range(n)], fill=(120, 220, 160), width=1)
    step = 1 if n / fps <= 40 else 5 if n / fps <= 200 else 30
    for t in range(0, int(n / fps) + 1, step):
        d.text((x(t * fps) - 6, H - pad + 4), str(t), fill=(150, 150, 150), font=f)
    legend = '黄：平均亮度　绿：运动（帧差）　红：爆点　蓝：硬切　灰：镜头起点　底色：前 2 秒'
    perf = sc.get('performance')
    if perf:
        y = lambda frac: H - pad - (H - 2 * pad) * frac
        for name, pts, color in (('peers', perf.get('peers') or [], (150, 150, 150)), ('ours', retention_points(perf, n / fps), (255, 120, 200))):
            pts = [(t, v) for t, v in pts if v is not None and t * fps <= n + 1]
            for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
                if name == 'peers':  # 虚线
                    for k in range(10):
                        if k % 2 == 0:
                            a0, a1 = k / 10, (k + 1) / 10
                            d.line([x((t0 + (t1 - t0) * a0) * fps), y(v0 + (v1 - v0) * a0), x((t0 + (t1 - t0) * a1) * fps), y(v0 + (v1 - v0) * a1)], fill=color, width=2)
                else:
                    d.line([x(t0 * fps), y(v0), x(t1 * fps), y(v1)], fill=color, width=3)
            for t, v in pts:
                d.ellipse([x(t * fps) - 4, y(v) - 4, x(t * fps) + 4, y(v) + 4], fill=color)
                if name == 'ours':
                    d.text((x(t * fps) + 6, y(v) + (4 if v > 0.85 else -18)), f'{v:.0%}' if v >= 0.1 else f'{v:.1%}', fill=color, font=f)
        legend += '　粉：留存（0–100%）' + ('　灰虚线：同类作品' if perf.get('peers') else '')
    d.text((pad, 6), legend, fill=(200, 200, 200), font=f)
    im.save(out)


def retention_points(perf, duration_sec):
    """留存折线的点 (秒, 剩下的比例)：手抄的 retention + 由 2 秒跳出、5 秒完播、完播率推出的点（完播率放在片尾，是近似）"""
    pts = {0.0: 1.0}
    for t, v in perf.get('retention') or []:
        if v is not None:
            pts[float(t)] = float(v)
    if perf.get('bounce2s') is not None:
        pts.setdefault(2.0, 1 - perf['bounce2s'])
    if perf.get('retention5s') is not None:
        pts.setdefault(5.0, perf['retention5s'])
    # 片尾以完播率为准：从「观看趋势」图上目测的片尾点会偏高（ep58 图上 20 秒约 12%，完播率是 5.73%），
    # 所以完播率总是接在片尾，并去掉片长最后 10% 里手抄的点
    if perf.get('completion') is not None and duration_sec:
        pts = {t: v for t, v in pts.items() if t < 0.9 * duration_sec}
        pts[round(duration_sec, 2)] = perf['completion']
    return sorted(pts.items())


def retention_check(r, perf, sc, fps, n):
    """把每段留存下降对到当时在放的镜头和字幕上；只记录，不判对错"""
    pts = [(t, v) for t, v in retention_points(perf, n / fps) if t * fps <= n + 1]
    peers = dict((float(t), v) for t, v in perf.get('peers') or [])
    shots, caps = sc.get('shots') or [], sc.get('captions') or []
    rows = []
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        rate = (v0 - v1) / v0 / max(t1 - t0, 1e-6)  # 这一段开头还在的人，每秒走掉多少
        f0, f1 = int(t0 * fps), int(t1 * fps)
        on = [s['name'] for s in shots if s['from'] < f1 and s['to'] > f0]
        said = [c['text'].split(' / ')[0] for c in caps if c['from'] < f1 and c['to'] > f0]
        rows.append({'from': t0, 'to': t1, 'left': v1, 'perSec': rate, 'peers': peers.get(t1), 'shots': on, 'captions': said})
    worst = max(rows, key=lambda x: x['perSec']) if rows else None
    lines = [f'{x["from"]:g}–{x["to"]:g} 秒：剩 {x["left"]:.1%}' + (f'（同类 {x["peers"]:.0%}）' if x['peers'] is not None else '') + f'，每秒走掉 {x["perSec"]:.1%}' + ('　← 掉得最快' if x is worst else '')
             + (f'；镜头 {"、".join(x["shots"][:4])}' if x['shots'] else '') + (f'；字幕「{"」「".join(x["captions"][:2])}」' if x['captions'] else '') for x in rows]
    head = '、'.join(s for s in [
        f'播放 {perf["plays"]}' if perf.get('plays') is not None else '',
        f'2 秒跳出 {perf["bounce2s"]:.1%}' if perf.get('bounce2s') is not None else '',
        f'完播 {perf["completion"]:.2%}' if perf.get('completion') is not None else '',
        f'评论 {perf["comments"]}' if perf.get('comments') is not None else '',
    ] if s)
    src = '，'.join(s for s in [perf.get('traffic'), perf.get('snapshotAt') and f'{perf["snapshotAt"]} 的数据', perf.get('source')] if s)
    r.add('retention', 'info', f'发布数据：{head}', f'来源：{src}。完播率画在片尾，是近似。\n' + '\n'.join(f'  - {l}' for l in lines), rows)


# —— 手机外放（单声道）——
def mono_check(r, video, fps):
    """手机外放、蓝牙小音箱常常把左右声道混成一个。相位相反的部分会互相抵消，声音突然变小甚至消失。
    比较「左右相加」和「左右各自」的能量：完全一样是 0 dB，普通立体声约 −3 dB，越往下抵消越多。"""
    try:
        raw = run(['ffmpeg', '-v', 'error', '-i', str(video), '-vn', '-ac', '2', '-ar', '48000', '-f', 'f32le', '-']).stdout
    except subprocess.CalledProcessError:
        return  # 没有音轨
    x = np.frombuffer(raw, np.float32).reshape(-1, 2).astype(np.float64)
    if not len(x):
        return
    L, R = x[:, 0], x[:, 1]
    if np.allclose(L, R, atol=1e-4):
        r.add('mono', 'pass', '左右声道一样，外放混成单声道不会变小')
        return
    loss = lambda l, rr: 10 * math.log10((np.mean(((l + rr) / 2) ** 2) + 1e-12) / (np.mean((l ** 2 + rr ** 2) / 2) + 1e-12))
    total = loss(L, R)
    sr, bad = 48000, []
    for i in range(0, len(L) - sr // 2, sr // 2):  # 半秒一窗
        l, rr = L[i:i + sr // 2], R[i:i + sr // 2]
        if 10 * math.log10(np.mean((l ** 2 + rr ** 2) / 2) + 1e-12) < -40:
            continue  # 太安静的段不算
        d = loss(l, rr)
        if d < -9:
            bad.append((i / sr, d))
    corr = float(np.corrcoef(L, R)[0, 1]) if np.std(L) > 0 and np.std(R) > 0 else 1.0
    lv = 'warn' if total < -4.5 or bad else 'pass'
    rows = '\n'.join(f'  - {t:.1f} 秒：混成单声道小了 {-d:.0f} dB' for t, d in bad[:15])
    r.add('mono', lv, f'混成单声道（手机外放）：整片小 {-total:.1f} dB' + (f'，{len(bad)} 段明显被抵消' if bad else ''),
          f'左右声道相关系数 {corr:.2f}（1 = 完全一样，0 = 互不相关，负数 = 相位相反）。普通立体声混成单声道约小 3 dB；整片超过 4.5 dB 或某段超过 9 dB 就要查是不是有反相的声音（加宽效果、反相的音效、立体声混响）。'
          + ('\n' + rows if rows else ''), {'lossDb': round(total, 2), 'correlation': round(corr, 3), 'segments': [[round(t, 2), round(d, 1)] for t, d in bad]})


# —— 和上一版比（--compare）——
def compare_reports(old_checks, new_checks):
    """按检查 id 对比两版：修好了（以前 ✗ ⚠，现在没有了或通过）、新出现（以前没有或通过，现在 ✗ ⚠）、还在"""
    bad = ('fail', 'warn')
    old = {c['id']: c for c in old_checks}
    new = {c['id']: c for c in new_checks}
    fixed = [old[i] for i in old if old[i]['level'] in bad and (i not in new or new[i]['level'] not in bad)]
    added = [new[i] for i in new if new[i]['level'] in bad and (i not in old or old[i]['level'] not in bad)]
    still = [new[i] for i in new if new[i]['level'] in bad and i in old and old[i]['level'] in bad]
    return fixed, added, still


def compare_check(r, path):
    p_ = Path(path)
    p_ = p_ / 'report.json' if p_.is_dir() else p_
    old = json.loads(p_.read_text(encoding='utf-8'))
    fixed, added, still = compare_reports(old['checks'], r.checks)
    fmt = lambda cs, mark: '\n'.join(f'  - {mark} {c["title"]}' for c in cs)
    lv = 'warn' if added else 'pass' if fixed or not still else 'info'
    r.add('compare', lv, f'和上一版比：修好 {len(fixed)} 项、新出现 {len(added)} 项、还剩 {len(still)} 项',
          f'上一版：{old.get("video")}（{p_}）' + ''.join(f'\n{name}：\n{fmt(cs, mark)}' for name, cs, mark in (('修好了', fixed, '✓'), ('新出现', added, '✗'), ('还在', still, '·')) if cs),
          {'fixed': [c['id'] for c in fixed], 'added': [c['id'] for c in added], 'still': [c['id'] for c in still], 'previous': str(p_)})


# —— 单文件 HTML 报告（图片内嵌，发给别人一个文件就能看）——
LEVEL_NAME = {'fail': '问题', 'warn': '提醒', 'skip': '没法查', 'pass': '通过', 'info': '记录'}


def _img_tag(path, alt):
    if not path or not Path(path).exists():
        return ''
    mime = 'image/png' if str(path).endswith('.png') else 'image/jpeg'
    data = base64.b64encode(Path(path).read_bytes()).decode('ascii')
    return f'<figure><img src="data:{mime};base64,{data}" alt="{_html.escape(alt)}" loading="lazy"><figcaption>{_html.escape(alt)}</figcaption></figure>'


def write_html(out, title, video, status, headline, checks, images):
    order = {'fail': 0, 'warn': 1, 'skip': 2, 'pass': 3, 'info': 4}
    rows = []
    for c in sorted(checks, key=lambda c: order[c['level']]):
        detail = _html.escape(c['detail'] or '').replace('\n', '<br>')
        rows.append(f'<details class="chk {c["level"]}" data-level="{c["level"]}"{" open" if c["level"] in ("fail", "warn") else ""}>'
                    f'<summary><span class="lv">{ICON[c["level"]]} {LEVEL_NAME[c["level"]]}</span>{_html.escape(c["title"])}<code>{_html.escape(c["id"])}</code></summary>'
                    + (f'<div class="detail">{detail}</div>' if detail else '') + '</details>')
    figs = _img_tag(images.get('frame0'), '第 0 帧（刷到时看到的）') + ''.join(_img_tag(p_, f'联系表 {i + 1}') for i, p_ in enumerate(images.get('sheets', [])))
    figs += _img_tag(images.get('hits'), '爆点前后：−16 / −3 / −1 / +2 帧') + _img_tag(images.get('frames'), '指定帧') + _img_tag(images.get('curve'), '亮度和运动曲线')
    counts = {k: sum(1 for c in checks if c['level'] == k) for k in order}
    filters = ''.join(f'<label><input type="checkbox" value="{k}" checked> {ICON[k]} {LEVEL_NAME[k]} {counts[k]}</label>' for k in order if counts[k])
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(title)} · video-lint</title><style>
:root{{--bg:#f6f6f4;--card:#fff;--ink:#1b1b1a;--muted:#77756f;--line:#e4e3de;--fail:#d03b3b;--warn:#c98500;--skip:#6250d6;--pass:#1b8f5a;--info:#8a8a85}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121211;--card:#1b1b1a;--ink:#ecebe7;--muted:#9b9993;--line:#2c2c2a;--fail:#e66767;--warn:#e0a020;--skip:#9085e9;--pass:#3fbf83}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:28px 16px 48px}}h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--muted);margin:0 0 16px;word-break:break-all}}
.status{{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:600;margin-bottom:12px}}.status.fail{{background:var(--fail);color:#fff}}.status.unmeasured{{background:var(--skip);color:#fff}}.status.pass{{background:var(--pass);color:#fff}}
.filters{{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0 14px;color:var(--muted)}}
.chk{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--info);border-radius:8px;margin:8px 0;padding:8px 12px}}
.chk.fail{{border-left-color:var(--fail)}}.chk.warn{{border-left-color:var(--warn)}}.chk.skip{{border-left-color:var(--skip)}}.chk.pass{{border-left-color:var(--pass)}}
summary{{cursor:pointer;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}.lv{{font-weight:600;white-space:nowrap}}summary code{{margin-left:auto;color:var(--muted);font-size:12px}}
.detail{{margin-top:6px;color:var(--muted);font-size:14px;white-space:normal}}figure{{margin:16px 0}}figure img{{max-width:100%;border-radius:8px;border:1px solid var(--line)}}figcaption{{color:var(--muted);font-size:13px}}
footer{{color:var(--muted);font-size:13px;margin-top:24px}}
</style></head><body><main>
<h1>{_html.escape(title)}</h1><p class="sub">{_html.escape(str(video))}</p>
<div class="status {status}">{_html.escape(headline)}</div>
<div class="filters">{filters}</div>
<section id="checks">{''.join(rows)}</section>
<h2>图</h2>{figs}
<footer>video-lint {VERSION} · 规则全是确定性的，数值只是信号，最后的观感和听感要人确认。</footer>
</main><script>
document.querySelectorAll('.filters input').forEach(b=>b.onchange=()=>{{const on=[...document.querySelectorAll('.filters input:checked')].map(x=>x.value);document.querySelectorAll('.chk').forEach(c=>c.style.display=on.includes(c.dataset.level)?'':'none')}});
</script></body></html>"""
    (out / 'report.html').write_text(page, encoding='utf-8')
    return out / 'report.html'


# —— 项目配置文件 .video-lint.toml ——
CONFIG_NAME = '.video-lint.toml'
# 配置文件里能写的键，和命令行同名（短横线换成下划线）；路径相对配置文件所在目录
CONFIG_KEYS = {'compare', 'platform', 'lufs', 'loud', 'strict', 'no_ocr', 'no_flash', 'captions', 'sidecar', 'sfx_audio', 'out'}
CONFIG_PATHS = {'compare', 'captions', 'sidecar', 'sfx_audio', 'out'}


def find_config(video, explicit=None):
    """--config 指定的；否则从视频所在目录往上找 .video-lint.toml，再看当前目录"""
    if explicit:
        return Path(explicit)
    for d in [Path(video).resolve().parent, *Path(video).resolve().parents, Path.cwd()]:
        f = d / CONFIG_NAME
        if f.is_file():
            return f
    return None


def load_config(path):
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib
    data = tomllib.loads(Path(path).read_text(encoding='utf-8'))
    unknown = set(data) - CONFIG_KEYS - {'ignore', 'levels'}
    if unknown:
        raise SystemExit(f'{path}：不认识的配置项 {sorted(unknown)}（能写的：{sorted(CONFIG_KEYS | {"ignore", "levels"})}）')
    for k in CONFIG_PATHS & set(data):
        data[k] = str((Path(path).parent / data[k]).resolve())
    if isinstance(data.get('loud'), list):
        data['loud'] = ','.join(data['loud'])
    return data


def merge_config(a, cfg):
    """命令行优先：命令行没给的才用配置文件里的"""
    for k in CONFIG_KEYS & set(cfg):
        if getattr(a, k, None) in (None, False):
            setattr(a, k, cfg[k])
    return a


def apply_overrides(r, cfg):
    """ignore：这些检查（支持通配符，如 hit.*）降成「只记录」；levels：把某项检查改成指定级别。都写进标题，报告里看得出来"""
    ignore = cfg.get('ignore', [])
    levels = cfg.get('levels', {})
    for c in r.checks:
        if any(fnmatch.fnmatchcase(c['id'], pat) for pat in ignore) and c['level'] != 'info':
            c['title'] = f'（配置里已忽略，原为 {c["level"]}）' + c['title']
            c['level'] = 'info'
            continue
        for pat, lv in levels.items():
            if fnmatch.fnmatchcase(c['id'], pat) and lv in ICON and c['level'] != lv:
                c['title'] = f'（配置改为 {lv}，原为 {c["level"]}）' + c['title']
                c['level'] = lv
                break


def main():
    ap = argparse.ArgumentParser(prog='video-lint', description='短视频成片质检 / deterministic QA for finished short videos')
    ap.add_argument('--version', action='version', version=f'video-lint {VERSION}')
    ap.add_argument('video')
    ap.add_argument('--sidecar')
    ap.add_argument('--config', help=f'配置文件；不给就从视频所在目录往上找 {CONFIG_NAME}')
    ap.add_argument('--out')
    ap.add_argument('--loud', help='要检查「有没有被配乐盖住」的音效名（逗号分隔，按子串匹配）')
    ap.add_argument('--lufs', type=float, help='整片响度目标（±1 以内算合格）。各风格不同：黑金 / 战术屏 −15.5、连线星图 −14、人生曲线 −10.8、口播 −16、暖纸 −19；不给就只记录')
    ap.add_argument('--strict', action='store_true', help='按整体状态给退出码：通过 0、有问题 2、有该查但查不了的项 3（1 留给脚本自己出错）')
    ap.add_argument('--platform', choices=['douyin', 'tiktok', 'reels', 'shorts', 'none'], help='竖屏联系表叠哪个平台的界面，默认取旁注的 platform（没有就是 douyin）')
    ap.add_argument('--sfx-audio', help='只有音效的分轨（wav，或者「无配乐仅音效」版 mp4）；有它就用音效 / 配乐能量比判断听不听得见')
    ap.add_argument('--text-dir',help='文字层 PNG 目录（textlayer.mjs 的输出），有它才自动查遮挡区和出画')
    ap.add_argument('--captions', help='字幕文件（.srt / .vtt / .ass），用来查阅读速度、开头第一句；会覆盖旁注里的字幕')
    ap.add_argument('--compare', help='上一版的 report.json（或报告目录）：列出修好了、新出现、还在的问题')
    ap.add_argument('--no-flash', action='store_true', help='不做闪烁安全检查（WCAG 2.3.1）')
    ap.add_argument('--no-ocr', action='store_true', help='没有文字层时也不用 OCR 找字')
    ap.add_argument('--text-failed', help='（run.mjs 用）文字层渲染失败的原因')
    ap.add_argument('--frames', help='另外抽这几帧拼成 frames.jpg（每行 4 格），看动画过程用：120,135=中段,150')
    a = ap.parse_args()
    cfg_path = find_config(a.video, a.config)
    cfg = load_config(cfg_path) if cfg_path else {}
    merge_config(a, cfg)

    video = Path(a.video)
    sc = json.loads(Path(a.sidecar).read_text(encoding='utf-8')) if a.sidecar else {}
    out = Path(a.out) if a.out else video.with_name(video.stem + '-lint')
    out.mkdir(parents=True, exist_ok=True)
    r = Report()
    images = {}

    # —— 基本信息 ——
    P = probe(video)
    fps = P['fps']
    if a.captions:
        sc['captions'] = parse_captions(a.captions, fps)
    mean, p995, diff, bright = frame_series(video, P['width'], P['height'])
    n = len(mean)
    portrait = P['height'] > P['width']
    meta = f"{P['width']}×{P['height']}，{fps:g} fps，{n} 帧（{n / fps:.2f} 秒），{'有' if P['audio'] else '没有'}音轨"
    issues = []
    if sc.get('fps') and abs(sc['fps'] - fps) > 0.01:
        issues.append(f"旁注 fps 是 {sc['fps']}")
    if sc.get('durationFrames') and abs(sc['durationFrames'] - n) > 1:
        issues.append(f"旁注时长是 {sc['durationFrames']} 帧（差 {n - sc['durationFrames']} 帧）")
    r.add('meta', 'warn' if issues else 'info', '基本信息', meta + ('；' + '；'.join(issues) if issues else ''), P | {'frames': n})
    if sc.get('width') and sc['width'] != P['width']:
        r.add('meta.size', 'info', '分辨率和旁注不同', f"旁注是 {sc['width']}×{sc.get('height')}（小样按比例缩小渲染时正常）")
    for note in sc.get('notes', []):
        r.add('sidecar.note', 'info', '旁注', note)

    # —— 第 0 帧和前 2 秒 ——
    f0 = np.asarray(extract_frames(video, [0], P['width'], out)[0].convert('L'), np.uint8)
    visible = float((f0 > 60).mean())
    first_visible = next((i for i in range(n) if p995[i] >= BLACK_P995), None)
    settle = float(np.median(mean[int(0.5 * fps):int(2 * fps)])) if n > 2 * fps else float(mean.mean())
    if np.percentile(f0, 99.9) < BLACK_P995:
        r.add('open.frame0', 'fail', '第 0 帧是黑屏', f'第一个有画面的帧是第 {first_visible} 帧（{(first_visible or 0) / fps:.2f} 秒）。刷到时看到的就是这一帧，黑屏会直接划走。', {'firstVisible': first_visible})
    elif visible < 0.005:
        # ep52 的第 0 帧就是这样：只有背景光晕，2 秒跳出 40%
        r.add('open.frame0', 'fail', '第 0 帧几乎是空画面', f'亮于 60 的像素只占 {visible:.2%}。刷到时看到的就是这一帧，第 0 帧要给完整画面。', {'visible': visible})
    elif mean[0] < 0.4 * settle:
        r.add('open.frame0', 'warn', '第 0 帧明显比后面暗（淡入开场）', f'第 0 帧平均亮度 {mean[0]:.1f}，0.5–2 秒的中位数 {settle:.1f}', {'mean0': float(mean[0]), 'settle': settle})
    else:
        r.add('open.frame0', 'pass', '第 0 帧有完整画面', f'亮于 60 的像素占 {visible:.1%}，平均亮度 {mean[0]:.1f}（0.5–2 秒中位数 {settle:.1f}）')
    # 一个缺陷只报一次（学 takecheck）：第 0 帧已经判了，这里不再算它
    frame0_flagged = r.checks[-1]['level'] in ('fail', 'warn')
    black_open = [i for i in range(min(n, int(2 * fps))) if p995[i] < BLACK_P995 and not (i == 0 and frame0_flagged)]
    if black_open:
        r.add('open.black', 'warn', '前 2 秒里有黑帧', f'共 {len(black_open)} 帧，从第 {black_open[0]} 帧起', {'frames': black_open})
    if n > 4 * fps:
        # 只记录不判对错：前 2 秒的运动量和全片中位数比，留着以后和抖音留存曲线对照
        m_open, m_all = float(diff[1:int(2 * fps)].mean()), float(np.median(diff[1:]))
        r.add('open.motion', 'info', f'前 2 秒运动量是全片中位数的 {m_open / max(m_all, 1e-3):.1f} 倍', f'前 2 秒平均帧差 {m_open:.3f}，全片中位数 {m_all:.3f}', {'open': m_open, 'median': m_all})

    # —— 黑场、静止 ——
    blacks = [(s, e) for s, e in runs(p995 < BLACK_P995, 3) if s > 0]
    mid = [(s, e) for s, e in blacks if e < n - 1]
    if mid:
        r.add('black', 'warn', '片中有黑场', '；'.join(f'{s / fps:.2f}–{e / fps:.2f} 秒（{shot_at(sc.get("shots", []), s)}）' for s, e in mid), [[s, e] for s, e in mid])
    elif blacks:
        r.add('black', 'info', '只有片尾黑场', f'{blacks[-1][0] / fps:.2f} 秒起')
    else:
        r.add('black', 'pass', '没有黑场')
    # 黑场本身也不动：和黑场重叠的静止段只算黑场，不重复报
    all_black = runs(p995 < BLACK_P995, 1)
    stills = [(s, e) for s, e in runs(diff < STATIC_DIFF, int(fps)) if not any(bs < e and s < be for bs, be in all_black)]
    if stills:
        early = [s for s in stills if s[0] < 3 * fps]
        r.add('static', 'warn' if early else 'info', '画面几乎不动超过 1 秒的段' + ('（前 3 秒就有）' if early else ''),
              '；'.join(f'{s / fps:.2f}–{e / fps:.2f} 秒（{shot_at(sc.get("shots", []), s)}）' for s, e in stills), [[s, e] for s, e in stills])

    # —— 字幕阅读速度 ——
    if sc.get('captions'):
        reading_speed_check(r, sc['captions'], fps)

    # —— 上传规格 ——
    spec_check(r, video, a.platform or sc.get('platform', 'none'))

    # —— 闪烁安全 ——
    if not a.no_flash:
        flash_check(r, video, P, fps, sc.get('shots') or [])

    # —— 爆点：收暗 → 静止 → 亮起 ——
    hit_tiles = []
    for h in sc.get('hits', []):
        H = h['frame']
        if not (40 <= H < n - 5):
            r.add(f'hit.{h["name"]}', 'skip', f'爆点 {h["name"]} 不在片内，没法查', f'第 {H} 帧；片长 {n} 帧')
            continue
        df = h.get('darkFrom')
        pre = float(mean[max(0, df - 10):df].mean()) if df and df > 10 else float(mean[H - 40:H - 30].mean())
        dark = float(mean[H - 6:H].mean())
        post = float(mean[H + 1:H + 5].mean())
        notes, level = [], 'pass'
        if df:
            ok = dark <= pre * 0.85
            notes.append(f'收暗 {"✓" if ok else "✗"}（{pre:.1f} → {dark:.1f}）')
            level = level if ok else 'warn'
        else:
            notes.append(f'爆点前亮度 {pre:.1f} → {dark:.1f}（旁注没给收暗起点，只记录）')
        if h.get('freeze'):
            k = h['freeze']
            moving = [round(float(diff[i]), 2) for i in range(H - k + 1, H)]
            ok = all(x < 0.3 for x in moving)
            notes.append(f'最后 {k} 帧静止 {"✓" if ok else "✗"}（帧差 {moving}）')
            level = level if ok else 'warn'
        burst = post >= dark * 1.2 + 1 or float(p995[H + 1:H + 5].mean()) >= float(p995[H - 6:H].mean()) + 20
        notes.append(f'亮起 {"✓" if burst else "✗"}（{dark:.1f} → {post:.1f}）')
        level = level if burst else 'warn'
        r.add(f'hit.{h["name"]}', level, f'爆点 {h["name"]}（第 {H} 帧，{H / fps:.2f} 秒）', '，'.join(notes))
        hit_tiles.append((h['name'], H, [H - 16, H - 3, H - 1, H + 2]))

    # —— 硬切和拍子 ——
    cuts = hard_cuts(diff)
    beats = (sc.get('beats') or {}).get('times')
    grid_src = (sc.get('beats') or {}).get('source', '旁注')
    if not beats and P['audio'] and n > 2 * fps:
        try:
            import librosa

            y, sr = audio_mono(video, 22050)
            _, bt = librosa.beat.beat_track(y=y, sr=sr, units='time')
            beats, grid_src = [float(t) for t in bt], '由音频估计（可能差半拍或一倍速）'
        except Exception as e:  # noqa: BLE001 没装 librosa 也照样出其它检查
            r.add('beats', 'info', '没有拍子网格', f'旁注里没有 beats，音频估计也失败了：{e}')
    if beats and cuts:
        bt = np.array(beats)
        period = float(np.median(np.diff(bt))) if len(bt) > 1 else 0.5
        rows, off = [], []
        for c in cuts:
            t = c / fps
            o = (t - bt[np.abs(bt - t).argmin()]) * fps
            half = abs(((t - bt[0]) / (period / 2)) - round((t - bt[0]) / (period / 2))) * (period / 2) * fps
            if abs(o) > 1.5:
                off.append(c)
                rows.append(f'第 {c} 帧（{t:.2f} 秒，{shot_at(sc.get("shots", []), c) or "—"}）偏 {o:+.1f} 帧' + ('，落在半拍上' if half <= 1.5 else ''))
        level = 'pass' if not off else 'warn' if len(off) <= max(2, len(cuts) // 4) else 'fail'
        if level == 'fail' and grid_src.startswith('由音频估计'):
            level = 'warn'  # 音频估的网格拍长有误差，越往后漂得越多（ep61：估成 0.464 秒，实际 0.4525），不够判 ✗
        r.add('beats.cuts', level, f'硬切卡拍：{len(cuts) - len(off)} / {len(cuts)} 处在拍点 ±1.5 帧内',
              f'拍子来源：{grid_src}，拍长 {period:.3f} 秒。' + ('不在拍上的：\n' + '\n'.join(f'  - {x}' for x in rows) if rows else ''), {'cuts': cuts, 'offBeat': off})
    elif cuts:
        r.add('beats.cuts', 'skip', f'检测到 {len(cuts)} 处硬切，没有拍子网格可比', '旁注里没有 beats，也没能从音频估出来', {'cuts': cuts})
    else:
        r.add('beats.cuts', 'info', '没检测到硬切', '叠化转场不算硬切')

    # —— 音频 ——
    if P['audio']:
        L = loudness(video)
        off = abs(L['I'] - a.lufs) if a.lufs is not None else 0
        lv = 'fail' if L['TP'] > 0 or off > 3 else 'warn' if L['TP'] > -1.0 or off > 1 else 'pass' if a.lufs is not None else 'info'
        r.add('audio.loudness', lv, f'整片 {L["I"]} LUFS、真峰值 {L["TP"]} dBFS、响度范围 {L["LRA"]} LU',
              (f'目标 {a.lufs} ±1 LUFS' if a.lufs is not None else '没给 --lufs，响度只记录') + '；真峰值不能高于 −1.0 dBFS', L)
        y, sr = audio_mono(video)
        open_db = db(y[: int(0.5 * sr)])
        mono_check(r, video, fps)
        r.add('audio.open', 'warn' if open_db < -50 else 'info', f'开头 0.5 秒的音量 {open_db:.1f} dBFS' + ('，几乎无声' if open_db < -50 else ''))
        names = [s for s in (a.loud.split(',') if a.loud else LOUD_DEFAULT) if s]
        checked, weak = 0, []
        if a.sfx_audio and sc.get('sfx'):
            sfx_stem_check(r, sc, y, sr, a.sfx_audio, fps, a.loud.split(',') if a.loud else None, out)
        for s in ([] if a.sfx_audio else sc.get('sfx', [])):
            if not any(k in s['name'] for k in names):
                continue
            t = s['frame'] / fps
            if t < 0.05 or t > len(y) / sr - 0.15:
                continue
            checked += 1
            seg = lambda t0, d: y[int(max(0, t0) * sr):int(max(0, t0 + d) * sr)]
            pre_t = max(0.0, t - 0.3)
            rise = db(seg(t, 0.15)) - db(seg(pre_t, min(0.15, t - pre_t)))
            rise_hi = band_db(seg(t, 0.15), sr) - band_db(seg(pre_t, min(0.15, t - pre_t)), sr)
            if not (rise >= 4 or rise_hi >= 6):
                weak.append({'name': s['name'], 'frame': s['frame'], 'volume': s.get('volume'), 'rise': round(rise, 1), 'riseHi': round(rise_hi, 1)})
        if checked:
            # 这条规则是在黑金 / 战术屏两种风格上校准的；音效正好落在配乐重拍上时，「前一刻」也很响，会误报。没指定 --loud 就只当参考
            lv = ('warn' if weak else 'pass') if a.loud else 'info'
            r.add('audio.sfx', lv, f'要听得见的音效：{checked - len(weak)} / {checked} 处比前一刻明显高出' + ('' if a.loud else '（默认清单，仅供参考）'),
                  '标准：全频高出 ≥ 4 dB，或 1.2–5 kHz 高出 ≥ 6 dB。' +('偏弱的（加音量，或挪开前面的音效）：\n' + '\n'.join(f'  - {w["name"]} @ {w["frame"] / fps:.2f} 秒（音量 {w["volume"]}）：全频 {w["rise"]:+} dB，1.2–5 kHz {w["riseHi"]:+} dB' for w in weak) if weak else ''), weak)
        elif sc.get('sfx') and not a.sfx_audio:  # 用了分轨就由 sfx_stem_check 出结论，这里不再补一条同 id 的
            r.add('audio.sfx', 'info', '旁注里有音效，但没有一个名字匹配「要听得见」的清单', f'当前清单：{",".join(names)}；用 --loud 指定')
    else:
        r.add('audio', 'skip', '没有音轨，响度和音效没法查', '查的如果是静音版，换带配乐的成片再跑')

    # —— 联系表 ——
    shots = sc.get('shots') or []
    if shots:
        picks = [(min(n - 1, max(s['from'], s['to'] - 4)), s['name']) for s in shots]
        how = '每个镜头切走前第 4 帧（这时该出现的东西都应该已经出来了）'
    elif sc.get('captions'):
        picks = [(min(n - 1, max(c['from'], c['to'] - 4)), c['text'][:8]) for c in sc['captions']]
        how = '每句字幕结束前第 4 帧'
    else:
        picks = [(round(i * (n - 1) / 15), f'{i + 1}/16') for i in range(16)]
        how = '整片均匀取 16 帧'
    picks = [(0, '第0帧')] + [p for p in picks if p[0] > 0]
    tile_w = 270 if portrait else 480
    platform = a.platform or sc.get('platform', 'douyin')
    zones = platform_zones(platform) if portrait else None
    extra = []
    for item in (a.frames or '').split(','):
        if item.strip():
            f, _, name = item.strip().partition('=')
            extra.append((min(n - 1, max(0, int(f))), name))
    want = [f for f, _ in picks] + [f for _, _, fs in hit_tiles for f in fs if 0 <= f < n] + [f for f, _ in extra]
    boxes, boxes_from_ocr = {}, False
    if a.text_dir and (Path(a.text_dir) / 'frames.json').exists():
        boxes = text_boxes(a.text_dir, video, out)
        text_checks(r, boxes, zones, sc, fps, P['width'] / P['height'])
    elif a.text_failed:
        r.add('text', 'skip', '文字层没渲出来，遮挡区和出画没法自动查', a.text_failed)
    elif not a.no_ocr and (engine := ocr_engine()) is not None:
        # 没有文字层：用 OCR 在联系表那些帧和前 2 秒五帧上找字
        frames = sorted({f for f, _ in picks} | {min(n - 1, round(s * fps) - (1 if s == 2 else 0)) for s in (0, 0.5, 1, 1.5, 2)} | {f for f, _ in extra})
        boxes, boxes_from_ocr = ocr_boxes(engine, video, frames, out), True
        text_checks(r, boxes, zones, sc, fps, P['width'] / P['height'], source='画面 OCR（RapidOCR，小字、艺术字、低对比的字可能漏掉，图形偶尔被当成字）')
    else:
        r.add('text', 'info', '没有文字位置数据，遮挡区只画框给人看', '装上 rapidocr-onnxruntime 会自动用 OCR 找字；也可以传 --text-dir（所有文字渲成纯品红的同帧 PNG）')
    ims = extract_frames(video, want, tile_w, out)
    tiles = [label(ims[f], f'{name} f{f} {f / fps:.2f}s', zones, boxes.get(f)) for f, name in picks if f in ims]
    sheets = []
    for i in range(0, len(tiles), 16):
        p = out / f'sheet-{i // 16 + 1}.jpg'
        grid(tiles[i:i + 16], 4, p)
        sheets.append(str(p))
    images['sheets'] = sheets
    r.add('frames.sheet', 'info', f'联系表 {len(sheets)} 张，{len(tiles)} 帧', how + (f'；叠的是 {zones["name"]} 的界面（{zones["updated"]} 版，数据来自 reelsmith/safezone）：红框是遮挡区，绿色虚线框是推荐安全区，字幕和主体放在绿框里（需要人看）' if zones else ''))
    if hit_tiles:
        ht = [label(ims[f], f'{name} {f - H:+d} 帧', None) for name, H, fs in hit_tiles for f in fs if f in ims]
        grid(ht, 4, out / 'hits.jpg')
        images['hits'] = str(out / 'hits.jpg')
    if extra:
        grid([label(ims[f], f'{name or shot_at(shots, f)} f{f} {f / fps:.2f}s', zones, boxes.get(f)) for f, name in extra], 4, out / 'frames.jpg')
        images['frames'] = str(out / 'frames.jpg')
    if sc.get('performance'):
        retention_check(r, sc['performance'], sc, fps, n)
    open_feat = opening_rules(r, sc, boxes, bright, fps, n, P, from_ocr=boxes_from_ocr)
    # 第 0 帧原尺寸单独存一张：在信息流里刷到时看到的就是它（开头规则 6）
    extract_frames(video, [0], P['width'], out)[0].save(out / 'frame0.jpg', quality=90)
    images['frame0'] = str(out / 'frame0.jpg')
    draw_curve(mean, diff, fps, sc, cuts, out / 'curve.png')
    # 给跨期汇总表（table.mjs）用的画面特征
    caps = sc.get('captions') or []
    features = {'durationSec': round(n / fps, 2), 'frame0Visible': round(visible, 4), 'firstVisibleFrame': first_visible,
                'firstCaptionFrame': caps[0]['from'] if caps else None,
                'openMotionRatio': round(float(diff[1:int(2 * fps)].mean()) / max(float(np.median(diff[1:])), 1e-3), 2) if n > 4 * fps else None,
                'portrait': portrait, **open_feat}
    images['curve'] = str(out / 'curve.png')

    # —— 和上一版比 ——
    if a.compare:
        compare_check(r, a.compare)

    # —— 写报告 ——
    if cfg:
        apply_overrides(r, cfg)
        r.add('config', 'info', f'用了配置文件 {cfg_path}', '、'.join(f'{k}={cfg[k]}' for k in sorted(cfg) if k not in ('ignore', 'levels')) + (f'；忽略 {cfg["ignore"]}' if cfg.get('ignore') else '') + (f'；改级别 {cfg["levels"]}' if cfg.get('levels') else ''))
    order = {'fail': 0, 'warn': 1, 'skip': 2, 'pass': 3, 'info': 4}
    summary = {k: r.count(k) for k in order}
    status = 'fail' if summary['fail'] else 'unmeasured' if summary['skip'] else 'pass'
    headline = f'{summary["fail"]} 个问题、{summary["warn"]} 个提醒、{summary["pass"]} 项通过' + (f'、{summary["skip"]} 项没法查' if summary['skip'] else '')
    md = [f'# video-lint 报告：{sc.get("id") or video.name}', '', f'成片：{video}', '',
          f'**{headline}**（整体：{ {"fail": "有问题", "unmeasured": "有项目没法查", "pass": "通过"}[status] }）', '']
    for c in sorted(r.checks, key=lambda c: order[c['level']]):
        md.append(f'- {ICON[c["level"]]} **{c["title"]}**' + (f'　{c["detail"]}' if c['detail'] and '\n' not in c['detail'] else ''))
        if c['detail'] and '\n' in c['detail']:
            md.append('  ' + c['detail'].replace('\n', '\n  '))
    md += ['', '## 图', '', f'- {images["frame0"]}（第 0 帧原尺寸，单独看：刷到时就是这一帧）'] + [f'- {p}' for p in sheets] + ([f'- {images["frames"]}（--frames 指定的帧）'] if 'frames' in images else []) +([f'- {images["hits"]}（每个爆点 −16 / −3 / −1 / +2 帧：应当是 收暗 → 静止 → 静止 → 亮起）'] if 'hits' in images else []) + [f'- {images["curve"]}', '',
           '数值只是信号，最后的观感和听感要人确认。']
    (out / 'report.md').write_text('\n'.join(md), encoding='utf-8')
    images['html'] = str(write_html(out, f'video-lint 报告：{sc.get("id") or video.name}', video, status, headline, r.checks, images))
    (out / 'report.json').write_text(json.dumps({'tool': 'video-lint', 'version': VERSION, 'video': str(video), 'sidecar': a.sidecar, 'status': status, 'summary': summary, 'features': features, 'performance': sc.get('performance'), 'checks': r.checks, 'images': images}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'{headline} → {out / "report.md"}')
    for c in r.checks:
        if c['level'] in ('fail', 'warn', 'skip'):
            print(f'  {ICON[c["level"]]} {c["title"]}')
    sys.exit(EXIT[status] if a.strict else 0)


if __name__ == '__main__':
    main()
