"""Agent Replay 列表响应压缩策略。"""

from dataclasses import replace

from app.core.replay.records import ReplayNode, ReplayTimeline


class ReplayCompactor:
    """压缩 Replay 列表摘要，避免 timeline 响应携带过长文本。"""

    def __init__(self, max_summary_chars: int = 200) -> None:
        """初始化 Replay 压缩器。

        参数:
            max_summary_chars: 单个节点摘要保留的最大字符数。

        返回:
            无。

        异常:
            ValueError: 如果 max_summary_chars 小于 1。

        副作用:
            保存压缩配置。
        """

        if max_summary_chars < 1:
            raise ValueError("max_summary_chars must be greater than zero")
        self._max_summary_chars = max_summary_chars

    def compact_timeline(self, timeline: ReplayTimeline) -> ReplayTimeline:
        """返回压缩后的 timeline 副本。

        参数:
            timeline: 原始 ReplayTimeline。

        返回:
            摘要被截断后的 ReplayTimeline。

        异常:
            无。

        副作用:
            无。不会修改原始 timeline 或 trace event。
        """

        return replace(
            timeline,
            nodes=[self.compact_node(node) for node in timeline.nodes],
        )

    def compact_node(self, node: ReplayNode) -> ReplayNode:
        """返回压缩后的节点副本。

        参数:
            node: 原始 ReplayNode。

        返回:
            摘要长度受限的 ReplayNode。

        异常:
            无。

        副作用:
            无。
        """

        if len(node.summary) <= self._max_summary_chars:
            return node
        if self._max_summary_chars <= 3:
            return replace(node, summary=node.summary[: self._max_summary_chars])
        return replace(node, summary=f"{node.summary[: self._max_summary_chars - 3]}...")
