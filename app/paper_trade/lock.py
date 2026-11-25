"""跨平台文件锁：POSIX 用 fcntl.flock，Windows 用 msvcrt.locking。

客户机里有 Windows 环境，`import fcntl` 在 Windows 上直接 ImportError，
所以纸面账本与日更任务都不能再在模块顶层硬依赖 fcntl。
"""
from __future__ import annotations

try:  # POSIX
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt


def acquire(fh) -> bool:
    """非阻塞抢排它锁；抢到返回 True，被占用返回 False。

    调用方必须长期持有 fh（不能是局部变量），否则文件句柄被回收时锁会提前释放。
    """
    if fcntl is not None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    try:
        fh.seek(0)
        if not fh.read(1):
            # Windows 的字节范围锁要求文件里至少有一个字节
            fh.write("\0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[name-defined]
        return True
    except OSError:
        return False


def release(fh) -> None:
    """释放锁并关闭句柄（幂等）。"""
    if fh is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        else:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[name-defined]
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass
