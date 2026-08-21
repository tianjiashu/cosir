from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_event import ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult


class ContextCompressListener(ContextListener):

    subAgent_need = True
    order = 0

    def __init__(self) -> None:
        pass

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        pass
