"""调用本机文件管理器打开路径。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class OpenError(RuntimeError):
    pass


def open_in_file_manager(path: Path) -> None:
    """在用户本机弹起 Finder / Explorer / xdg-open 打开 path。

    不等返回；找不到对应平台的命令时抛 OpenError，让上层决定如何反馈。
    """
    resolved = path.resolve()
    if sys.platform == "darwin":
        cmd = ["open", str(resolved)]
    elif sys.platform == "win32":
        cmd = ["explorer", str(resolved)]
    else:
        cmd = ["xdg-open", str(resolved)]

    try:
        subprocess.Popen(cmd)
    except FileNotFoundError as exc:
        raise OpenError(f"本机未安装可打开目录的命令：{cmd[0]}") from exc
    except OSError as exc:
        raise OpenError(f"启动 {cmd[0]} 失败：{exc}") from exc
