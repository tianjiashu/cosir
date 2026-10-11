"""按请求种类装配 human-in-the-loop 的领域决定处理器。"""

from collections.abc import Callable

from app.core.tools.schemas import UserDecision, UserInputRequest

DecisionHandler = Callable[[UserInputRequest, UserDecision], None]


class UserInputDecisionHandlers:
    """集中登记需要在通用决定投影前执行领域副作用的处理器。"""

    def __init__(self) -> None:
        self._handlers: dict[str, DecisionHandler] = {}

    def register(self, request_kind: str, handler: DecisionHandler) -> None:
        """在进程装配期登记请求类型处理器；重复装配同一契约视为程序错误。"""

        if request_kind in self._handlers:
            if self._handlers[request_kind] is handler:
                return
            raise ValueError(f"用户决定处理器已注册：{request_kind}")
        self._handlers[request_kind] = handler

    def handle_rejection(self, request: UserInputRequest, decision: UserDecision) -> None:
        """在驳回观察写入模型上下文前处理对应领域事实。"""

        handler = self._handlers.get(request.kind)
        if handler is not None:
            handler(request, decision)


