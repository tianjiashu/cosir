class ListenerResult:
    """上下文监听器的 usage 聚合结果。"""

    def __init__(self, usage: int) -> None:
        """初始化 listener 结果。

        参数:
            usage: 当前有效上下文的 token 占用。

        返回:
            无。

        异常:
            无。

        副作用:
            保存可由 listener 聚合更新的 usage。
        """
        self.usage = usage
