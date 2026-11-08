"""不安装也能直接跑：python -I lint.py <视频> …（代码在 video_lint/lint.py）"""
import sys
from pathlib import Path

# -I（隔离模式）不会把脚本所在目录放进 sys.path，这里手动加上，才找得到旁边的 video_lint 包
sys.path.insert(0, str(Path(__file__).resolve().parent))

from video_lint.lint import main  # noqa: E402

if __name__ == '__main__':
    main()
