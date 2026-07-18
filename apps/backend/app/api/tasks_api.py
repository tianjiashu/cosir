"""浠诲姟鍩熺鐐广€?

鍖呭惈浠诲姟鏌ヨ銆佷簨浠躲€乧heckpoint 涓庡彇娑堢鐐广€傛墍鏈夌鐐归€氳繃
妯″潡绾?``@app.*`` 瑁呴グ鍣ㄧ洿鎺ユ敞鍐屽埌 ``app.api.app.app`` 鍗曚緥涓娿€?
"""

from fastapi import Depends, HTTPException

from app.api.app import app
from app.api.dependencies import get_runtime
from app.core.runtime.runner import AgentRuntime


@app.get("/health")
async def get_health(runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """杩斿洖鍚庣鍋ュ悍鐘舵€佷笌褰撳墠妯″瀷閰嶇疆鎽樿銆?

    鍙傛暟:
        runtime: 閫氳繃渚濊禆娉ㄥ叆鐨勮繍琛屾椂鍗曚緥銆?

    杩斿洖:
        涓嶅惈 secret 鍘熸枃鐨勫仴搴风姸鎬佸瓧鍏搞€?

    寮傚父:
        鏃犮€?

    鍓綔鐢?
        鏃犮€?
    """

    return runtime.backend_health()


@app.get("/tasks/{task_id}")
async def get_task(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """杩斿洖浠诲姟鐘舵€併€?

    鍙傛暟:
        task_id: 鏉ヨ嚜璺敱鐨勪换鍔℃爣璇嗐€?
        runtime: 閫氳繃渚濊禆娉ㄥ叆鐨勮繍琛屾椂鍗曚緥銆?

    杩斿洖:
        宸插瓨鍌ㄧ殑浠诲姟鐘舵€併€?

    寮傚父:
        HTTPException: 褰撲换鍔′笉瀛樺湪鏃舵姏鍑恒€?

    鍓綔鐢?
        鏃犮€?
    """

    try:
        return runtime.get_task(task_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.get("/tasks/{task_id}/events")
async def list_events(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> list:
    """杩斿洖浠诲姟鐨勮繍琛屾椂浜嬩欢銆?

    鍙傛暟:
        task_id: 鏉ヨ嚜璺敱鐨勪换鍔℃爣璇嗐€?
        runtime: 閫氳繃渚濊禆娉ㄥ叆鐨勮繍琛屾椂鍗曚緥銆?

    杩斿洖:
        璇ヤ换鍔＄殑鏈夊簭浜嬩欢鍒楄〃銆?

    寮傚父:
        HTTPException: 褰撲换鍔′笉瀛樺湪鏃舵姏鍑恒€?

    鍓綔鐢?
        鏃犮€?
    """

    try:
        return [event.to_dict() for event in runtime.list_events(task_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc


@app.get("/tasks/{task_id}/turns")
async def list_turns(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> list:
    """杩斿洖浠诲姟涓嬬殑杞鍒楄〃銆?

    鍙傛暟:
        task_id: 鏉ヨ嚜璺敱鐨勪换鍔℃爣璇嗐€?
        runtime: 閫氳繃渚濊禆娉ㄥ叆鐨勮繍琛屾椂鍗曚緥銆?

    杩斿洖:
        璇ヤ换鍔′笅鐨勬湁搴忚疆娆″垪琛ㄣ€?

    寮傚父:
        HTTPException: 褰撲换鍔′笉瀛樺湪鏃舵姏鍑恒€?

    鍓綔鐢?
        鏃犮€?
    """

    try:
        return [turn.to_dict() for turn in runtime.list_turns(task_id)]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc



@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """灏嗕换鍔℃爣璁颁负宸插彇娑堛€?

    鍙傛暟:
        task_id: 鏉ヨ嚜璺敱鐨勪换鍔℃爣璇嗐€?
        runtime: 閫氳繃渚濊禆娉ㄥ叆鐨勮繍琛屾椂鍗曚緥銆?

    杩斿洖:
        鏇存柊鍚庣殑浠诲姟鐘舵€併€?

    寮傚父:
        HTTPException: 褰撲换鍔′笉瀛樺湪鏃舵姏鍑恒€?

    鍓綔鐢?
        鍦ㄨ繍琛屾椂瀛樺偍涓洿鏂颁换鍔＄姸鎬併€?
    """

    try:
        task = runtime.cancel_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    return task.to_dict()
