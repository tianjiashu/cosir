"""用户输入进入调度之前被拒时的领域异常。

入队前的输入问题（模型配置没了、附件绑定失效、模型不支持所选能力）必须和「寻址不到 Task」
区分开：后者按仓库约定是 ``KeyError`` → 404，前者是用户可修正的输入问题。两者共用 ``ValueError``
时，接入层只能回一句「当前对话状态不允许该操作，请刷新后重试」，既误导又不可排查。
"""


class UserInputRejectedError(ValueError):
    """用户输入在入队前被拒。

    ``code`` 是稳定的机器码（前端可分支），``message`` 面向用户，``str(error)`` 保留诊断细节
    （附件 id、缺失资源等）只进日志。三者分开是为了让「用户看到的提示」和「日志里的事实」各自
    成立——不要再出现「用户看到状态冲突、日志里什么都没有」。
    """

    def __init__(self, code: str, message: str, *, detail: str | None = None) -> None:
        super().__init__(detail or message)
        self.code = code
        self.message = message


__all__ = ["UserInputRejectedError"]
