"""LangGraph graph 构建边界。"""

from typing import Any, Callable


def build_react_like_graph(factory: Callable[[], Any]) -> Any:
    """通过外部工厂构建默认 ReAct-like graph。

    参数:
        factory: 延迟创建 LangGraph graph 的工厂函数。

    返回:
        工厂返回的 graph 对象。

    异常:
        RuntimeError: 如果工厂没有返回 graph 对象。

    副作用:
        取决于传入工厂，可能构建 LangGraph 节点与边。
    """

    graph = factory()
    if graph is None:
        raise RuntimeError("graph factory returned None")
    return graph
