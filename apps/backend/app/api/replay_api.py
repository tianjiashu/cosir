"""Agent Replay 查询 API 路由。

端点以模块级 ``@app.get`` 直接注册到 ``app.api.app.app`` 单例上，
运行时通过 ``Depends(get_runtime)`` 注入，不再由注册函数包裹。
"""

from fastapi import Depends, HTTPException, Query

from app.api.app import app
from app.api.dependencies import get_runtime
from app.core.runtime.runner import AgentRuntime


@app.get("/runs/{run_id}/replay")
async def get_run_replay(
    run_id: str,
    runtime: AgentRuntime = Depends(get_runtime),
    include_debug: bool = Query(False),
) -> dict:
    """返回指定 run 的 Agent Replay timeline。

    参数:
        run_id: 路由中的 Durable Run 标识。
        runtime: 通过依赖注入的运行时单例。
        include_debug: 是否返回排查用 debug 信息。

    返回:
        replay timeline 字典。

    异常:
        HTTPException: Replay 服务不可用、run_id 非法或 run 不存在时抛出。

    副作用:
        只读查询 trace ledger，不消费审批恢复命令。
    """

    try:
        return runtime.replay_service().get_run_replay(run_id, include_debug=include_debug)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run replay not found") from exc


@app.get("/traces/{trace_id}/replay")
async def get_trace_replay(
    trace_id: str,
    runtime: AgentRuntime = Depends(get_runtime),
    include_debug: bool = Query(False),
) -> dict:
    """返回指定 trace 的 Agent Replay timeline。

    参数:
        trace_id: 路由中的 Trace 标识。
        runtime: 通过依赖注入的运行时单例。
        include_debug: 是否返回排查用 debug 信息。

    返回:
        replay timeline 字典。

    异常:
        HTTPException: Replay 服务不可用、trace_id 非法或 trace 不存在时抛出。

    副作用:
        只读查询 trace ledger。
    """

    try:
        return runtime.replay_service().get_trace_replay(trace_id, include_debug=include_debug)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="trace replay not found") from exc


@app.get("/replay/nodes/{node_id}")
async def get_replay_node(
    node_id: str,
    runtime: AgentRuntime = Depends(get_runtime),
    include_debug: bool = Query(True),
) -> dict:
    """返回单个 replay 节点详情。

    参数:
        node_id: 路由中的 replay_node_id。
        runtime: 通过依赖注入的运行时单例。
        include_debug: 是否返回排查用 debug 信息。

    返回:
        包含完整 payload 的 replay 节点字典。

    异常:
        HTTPException: Replay 服务不可用、node_id 非法或节点不存在时抛出。

    副作用:
        只读查询 trace ledger。
    """

    try:
        return runtime.replay_service().get_node_detail(node_id, include_debug=include_debug)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="replay node not found") from exc
