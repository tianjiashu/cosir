"""跨平台的进程观察：存活判断、后代列表、等待退出。供真进程用例断言「进程树清理干净了」。

不用 ``os.kill(pid, 0)`` 判断存活：Windows 上 ``os.kill`` 对 0 以外的信号一律调用 ``TerminateProcess``，
信号 0 也会把进程结束掉。Windows 用 Toolhelp 快照与 ``GetExitCodeProcess``（ctypes，不引入依赖），
POSIX 用 ``ps``。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time

_IS_WINDOWS = os.name == "nt"


def alive(pid: int) -> bool:
    """进程是否还在运行（POSIX 上僵尸也算还在：它的父进程还没回收它）。"""

    if _IS_WINDOWS:
        return _windows_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def descendants(pid: int) -> dict[int, str]:
    """``pid`` 的全部在世后代：PID → 可执行文件名。"""

    table = _process_table()
    children: dict[int, list[int]] = {}
    for child, (parent, _name) in table.items():
        children.setdefault(parent, []).append(child)
    found: dict[int, str] = {}
    pending = [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in found and child != pid:
                found[child] = table[child][1]
                pending.append(child)
    return found


def survivors(pids: list[int], timeout: float) -> list[int]:
    """等这些进程退出，返回到时限仍活着的。"""

    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if alive(pid)]
        if remaining:
            time.sleep(0.1)
    return remaining


def kill(pid: int) -> None:
    """只结束这一个进程（不含后代），已退出时什么也不做。"""

    if _IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, 9)


def _process_table() -> dict[int, tuple[int, str]]:
    if _IS_WINDOWS:
        return _windows_process_table()
    output = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True, check=True
    ).stdout
    table: dict[int, tuple[int, str]] = {}
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            table[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2]))
    return table


# --- Windows（ctypes） ---

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x2


def _windows_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_table() -> dict[int, tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [  # noqa: RUF012 — ctypes 结构体的字段声明
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(ProcessEntry)
    table: dict[int, tuple[int, str]] = {}
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    # 快照里的父 PID 可能已被复用（父进程早已退出）：只保留仍在世的条目，后代遍历才不会串到别的进程树。
    return {pid: value for pid, value in table.items() if _windows_alive(pid)}
