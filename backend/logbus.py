"""
统一日志总线（V1.5）

两档输出，便于「beta 详细排查」与「定型后只看主要动作」之间切换：
  - info(scope, msg)  主要动作：状态流转、成功/失败、入队/发布等 —— 始终打印。
  - debug(scope, msg) 细节：每一步、每次 API 调用、中间值 —— 仅当 verbose 开启时打印。

verbose 由配置项 log_verbose 控制（启动与每次保存设置时同步）。
beta 阶段默认 True（详细）；定型后在设置里关掉即只剩主要动作。

打印做多重 UTF-8 兜底：某些宿主 stdout 非 UTF-8 时，print 中文/符号可能
UnicodeEncodeError，绝不能因日志中断业务流程。
"""
import sys
import time

_verbose = True


def set_verbose(v: bool):
    global _verbose
    _verbose = bool(v)


def is_verbose() -> bool:
    return _verbose


def _emit(tag: str, msg: str):
    line = f"[{tag} {time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        try:
            sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
            sys.stdout.flush()
        except Exception:
            pass


def info(scope: str, msg: str):
    """主要动作，始终打印。"""
    _emit(scope, msg)


def debug(scope: str, msg: str):
    """细节日志，仅 verbose 开启时打印。"""
    if _verbose:
        _emit(scope + "·详", msg)
