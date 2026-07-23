"""让标准 logging 支持规范约定的 ``extra["msg"]`` 键。"""

import logging


def install_msg_relocation() -> None:
    """全局允许 ``extra["msg"]``：重定位到 ``display_message``，避免覆盖事件模板。

    Python 标准 logging 禁止 ``extra`` 中出现 ``msg``（它是 ``LogRecord`` 的保留属性，
    会直接抛 ``KeyError``）。规范约定调用方用 ``extra={"msg": ...}`` 写中文消息，
    因此这里在 ``makeRecord`` 阶段把 ``msg`` 重定位到 ``display_message``，既满足规范
    又不影响 ``event``（位置参数模板）提取。仅打补丁一次，对未使用 ``msg`` 的调用完全透明。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        替换 ``logging.Logger.makeRecord`` 为带重定位逻辑的版本（幂等）。
    """
    if getattr(logging.Logger.makeRecord, "_coding_agent_relocated", False):
        return
    original = logging.Logger.makeRecord

    def _make_record(
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: tuple,
        exc_info: object,
        func: str | None = None,
        extra: dict | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        clean_extra = dict(extra) if extra else None
        human_msg = None
        if clean_extra and "msg" in clean_extra:
            human_msg = clean_extra.pop("msg")
        record = original(self, name, level, fn, lno, msg, args, exc_info, func, clean_extra, sinfo)
        if human_msg is not None:
            record.display_message = human_msg
        return record

    _make_record._coding_agent_relocated = True  # type: ignore[attr-defined]
    logging.Logger.makeRecord = _make_record


log = logging.getLogger("coding_agent.backend")
