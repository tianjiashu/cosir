from app.models import RuntimeMessage


class ListenerResult:
    """上下文监听器结果。"""
    usage: int = 0
    messages_after_compressor: list[RuntimeMessage]

    def __init__(self, usage: int, messages_after_compressor: list[RuntimeMessage] = None):
        self.usage = usage
        self.messages_after_compressor = messages_after_compressor
