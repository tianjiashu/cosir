"""Windows Job Object ctypes 封装，用于子进程退出时强杀整个进程树。

本模块只封装 Win32 Job Object 系统调用，不感知任何工具语义；仅被
``app.tools.tool_execute.tool_executor`` 的子进程入口调用。非 Windows 平台
直接返回 ``False``（无操作）。
"""

import ctypes
import os
from typing import ClassVar

from app.config.logging.logger import log

# --- Win32 常量 ---
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, type]]] = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    # 字段顺序与对齐遵循 Win32 JOBOBJECT_BASIC_LIMIT_INFORMATION（64 位）。
    _fields_: ClassVar[list[tuple[str, type]]] = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, type]]] = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def assign_current_process_to_kill_on_close_job() -> bool:
    """将当前进程挂入一个 KILL_ON_JOB_CLOSE 的 Job Object。

    参数:
        无。

    返回:
        True 表示成功挂入 Job（进程退出时 OS 回收句柄，整棵进程树被强杀）；
        False 表示不可用（非 Windows、或任一步失败），降级为"无树杀保护"。

    异常:
        不抛出：底层 ctypes 调用失败仅记 warning 并返回 False，不阻断工具执行。

    副作用:
        创建一个 Job Object 并 AssignProcessToJobObject 当前进程；句柄故意不关闭，
        依赖"进程退出时 OS 自动回收句柄"触发树杀。重复调用会创建多个 Job，但子
        进程入口仅调用一次，无此路径。
    """
    if os.name != "nt":
        return False
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    kernel32 = windll.kernel32

    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = []

    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        log.warning(
            "windows_job_object_create_failed",
            extra={"msg": "创建 Job Object 失败，降级为无树杀保护", "data": {}},
        )
        return False

    extended = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    extended.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job_handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(extended),
        ctypes.sizeof(extended),
    )
    if not ok:
        log.warning(
            "windows_job_object_set_info_failed",
            extra={"msg": "设置 Job Object 扩展限制失败，降级为无树杀保护", "data": {}},
        )
        return False

    if not kernel32.AssignProcessToJobObject(job_handle, kernel32.GetCurrentProcess()):
        log.warning(
            "windows_job_object_assign_failed",
            extra={"msg": "进程挂入 Job Object 失败，降级为无树杀保护", "data": {}},
        )
        return False

    return True
