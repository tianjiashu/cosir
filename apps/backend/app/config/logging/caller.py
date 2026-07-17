"""计算日志 ``caller`` 字段（模块路径:类.方法:行号）。"""

from functools import lru_cache
import logging
import sys
from pathlib import Path

# app/config/logging/<file>.py -> parents[3] = apps/backend
_BACKEND_ROOT = Path(__file__).resolve().parents[3]

# 仅缓存非空类名：子进程回放时栈上找不到用户帧会得到空串，
# 不能把空串缓存住，否则会污染同进程后续同函数的类名解析。
_CLASS_CACHE: dict[tuple[str, str], str] = {}


@lru_cache(maxsize=512)
def _module_dotted_path(pathname: str) -> str:
    """把绝对路径转换为相对后端的 dotted 模块路径。

    参数:
        pathname: ``LogRecord.pathname`` 绝对路径。

    返回:
        形如 ``app.storage.crud.durable`` 的模块路径；无法相对后端根时原样返回。

    异常:
        无。

    副作用:
        无。
    """
    try:
        rel = Path(pathname).resolve().relative_to(_BACKEND_ROOT)
    except ValueError:
        return pathname
    rel_str = str(rel.with_suffix(""))
    if rel_str.endswith("__init__"):
        rel_str = rel_str[: -len("__init__")]
    return rel_str.replace("/", ".").replace("\\", ".")


def _resolve_class_name(frame) -> str:
    """从调用帧的局部变量推断类名。

    参数:
        frame: 调用 ``logger`` 的栈帧。

    返回:
        实例方法的类名、类方法的类名，或空字符串（模块级函数）。

    异常:
        无。

    副作用:
        无。
    """
    self_obj = frame.f_locals.get("self")
    if self_obj is not None and hasattr(self_obj, "__class__"):
        return type(self_obj).__name__
    cls_obj = frame.f_locals.get("cls")
    if isinstance(cls_obj, type):
        return cls_obj.__name__
    return ""


def _class_name_for(pathname: str, func_name: str) -> str:
    """按 ``(pathname, func_name)`` 定位调用帧并推断类名。

    参数:
        pathname: 日志调用所在文件路径。
        func_name: 日志调用所在函数名。

    返回:
        类名；定位失败或模块级函数返回空字符串。

    异常:
        无。

    副作用:
        仅在命中时写入进程级类名缓存（空串不缓存）。
    """
    cached = _CLASS_CACHE.get((pathname, func_name))
    if cached is not None:
        return cached
    frame = sys._getframe(1)
    while frame is not None:
        code = frame.f_code
        if code.co_filename == pathname and code.co_name == func_name:
            class_name = _resolve_class_name(frame)
            if class_name:
                _CLASS_CACHE[(pathname, func_name)] = class_name
            return class_name
        frame = frame.f_back
    return ""


def compute_caller(record: logging.LogRecord) -> str:
    """计算 ``caller`` 字段：``模块路径:类.方法:行号``。

    参数:
        record: Python logging 传入的日志记录。

    返回:
        形如 ``app.storage.crud.durable:DurableRunStore.save:101`` 的调用位置；
        无法解析类名时退化为 ``模块:方法:行号``。

    异常:
        无。任何异常都被吞掉并退化为原始路径信息。

    副作用:
        无。
    """
    try:
        module = _module_dotted_path(record.pathname)
        class_name = _class_name_for(record.pathname, record.funcName)
        method = f"{class_name}.{record.funcName}" if class_name else record.funcName
        return f"{module}:{method}:{record.lineno}"
    except Exception:
        return f"{getattr(record, 'pathname', '?')}:{getattr(record, 'funcName', '?')}:{getattr(record, 'lineno', 0)}"


class CallerFilter(logging.Filter):
    """为每条 LogRecord 自动注入 ``caller`` 调用位置字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """把调用位置写入 ``record.caller``。

        参数:
            record: Python logging 传入的 LogRecord。

        返回:
            始终返回 True，表示不过滤日志。

        异常:
            无。

        副作用:
            修改 LogRecord 的 ``caller`` 属性。
        """
        record.caller = compute_caller(record)
        return True
