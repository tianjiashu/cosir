"""瀛樺偍鍚庣浣跨敤鐨勬寔涔呭寲璁板綍鍊煎璞°€?"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


def datetime_to_text(value: datetime) -> str:
    """灏?datetime 鍊艰浆鎹负 ISO-8601 鏂囨湰銆?

    鍙傛暟:
        value: 寰呭簭鍒楀寲鐨?datetime 鍊笺€?

    杩斿洖:
        ISO-8601 datetime 瀛楃涓层€?

    寮傚父:
        鏃犮€?

    鍓綔鐢?
        鏃犮€?
    """

    return value.isoformat()


@dataclass
class WorkspaceRecord:
    """琛ㄧず涓€涓湰鍦板伐浣滃尯銆?

    鍙傛暟:
        workspace_id: 鍞竴鐨勫伐浣滃尯鏍囪瘑绗︺€?
        name: 鐢ㄦ埛鍙鐨勫伐浣滃尯鍚嶇О銆?
        root_path: 宸ヤ綔鍖虹殑鏈湴鏂囦欢绯荤粺璺緞銆?
        created_at: 宸ヤ綔鍖哄垱寤烘椂鐨勬椂闂存埑銆?
        updated_at: 宸ヤ綔鍖烘渶杩戞洿鏂版椂鐨勬椂闂存埑銆?

    杩斿洖:
        涓€涓伐浣滃尯鐘舵€佽褰曘€?

    寮傚父:
        鏃犮€?

    鍓綔鐢?
        鏃犮€?
    """

    workspace_id: str
    name: str
    root_path: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, str]:
        """灏嗗伐浣滃尯鐘舵€佽浆鎹负鍙簭鍒楀寲涓?JSON 鐨勫瓧鍏搞€?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            宸ヤ綔鍖虹姸鎬佺殑瀛楀吀琛ㄧず銆?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            鏃犮€?
        """

        return {
            "workspace_id": self.workspace_id,
            "name": self.name,
            "root_path": self.root_path,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }


@dataclass
class TaskRecord:
    """琛ㄧず涓€涓?Agent 浠诲姟鐨勬寔涔呭寲鐘舵€併€?

    鍙傛暟:
        task_id: 鍞竴鐨勪换鍔℃爣璇嗙銆?
        workspace_id: 涓庝换鍔″叧鑱旂殑宸ヤ綔鍖烘爣璇嗙銆?
        agent_id: 璐熻矗鎵ц浠诲姟鐨?Agent 鏍囪瘑绗︺€?
        input_text: 鍘熷鐨勭函鏂囨湰鐢ㄦ埛浠诲姟銆?
        title: 浠诲姟瀹瑰櫒鏍囬銆?
        last_message_preview: 鏈€杩戠敤鎴疯緭鍏ユ憳瑕併€?
        latest_turn_id: 鏈€杩戜竴娆¤疆娆℃爣璇嗙銆?
        status: 褰撳墠浠诲姟鐘舵€併€?
        created_at: 浠诲姟鍒涘缓鏃剁殑 UTC 鏃堕棿鎴炽€?
        updated_at: 浠诲姟鏈€杩戞洿鏂版椂鐨?UTC 鏃堕棿鎴炽€?

    杩斿洖:
        涓€涓彲鍙樼殑浠诲姟鐘舵€佽褰曘€?

    寮傚父:
        鏃犮€?

    鍓綔鐢?
        鏃犮€?
    """

    task_id: str
    workspace_id: str
    agent_id: str
    input_text: str
    title: str
    last_message_preview: str
    latest_turn_id: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, Optional[str]]:
        """灏嗕换鍔＄姸鎬佽浆鎹负鍙簭鍒楀寲涓?JSON 鐨勫瓧鍏搞€?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            浠诲姟鐘舵€佺殑瀛楀吀琛ㄧず銆?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            鏃犮€?
        """

        return {
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "agent_id": self.agent_id,
            "input_text": self.input_text,
            "title": self.title,
            "last_message_preview": self.last_message_preview,
            "latest_turn_id": self.latest_turn_id,
            "status": self.status,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }


@dataclass
class TurnRecord:
    """琛ㄧず涓€娆＄敤鎴蜂笌 Agent 鐨勮疆娆°€?

    鍙傛暟:
        turn_id: 鍞竴鐨勮疆娆℃爣璇嗙銆?
        task_id: 涓庤杞鍏宠仈鐨勪换鍔℃爣璇嗙銆?
        input_text: 璇ヨ疆娆＄殑鐢ㄦ埛鏂囨湰銆?
        status: 褰撳墠杞鐘舵€併€?
        created_at: 杞鍒涘缓鏃剁殑鏃堕棿鎴炽€?
        updated_at: 杞鏈€杩戞洿鏂版椂鐨勬椂闂存埑銆?

    杩斿洖:
        涓€涓疆娆＄姸鎬佽褰曘€?

    寮傚父:
        鏃犮€?

    鍓綔鐢?
        鏃犮€?
    """

    turn_id: str
    task_id: str
    input_text: str
    status: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, str]:
        """灏嗚疆娆＄姸鎬佽浆鎹负鍙簭鍒楀寲涓?JSON 鐨勫瓧鍏搞€?

        鍙傛暟:
            鏃犮€?

        杩斿洖:
            杞鐘舵€佺殑瀛楀吀琛ㄧず銆?

        寮傚父:
            鏃犮€?

        鍓綔鐢?
            鏃犮€?
        """

        return {
            "turn_id": self.turn_id,
            "task_id": self.task_id,
            "input_text": self.input_text,
            "status": self.status,
            "created_at": datetime_to_text(self.created_at),
            "updated_at": datetime_to_text(self.updated_at),
        }


@dataclass
class StepRecord:
    """琛ㄧず涓€涓寔涔呭寲鐨勮繍琛屾椂姝ラ銆?

    鍙傛暟:
        step_id: 鍞竴鐨勬楠ゆ爣璇嗙銆?
        turn_id: 涓庤姝ラ鍏宠仈鐨勮疆娆℃爣璇嗙銆?
        step_type: 杩愯鏃舵楠ょ被鍨嬨€?
        status: 褰撳墠姝ラ鐘舵€併€?
        input_summary: 鐢ㄤ簬璇婃柇鐨勭畝鐭緭鍏ユ憳瑕併€?
        output_summary: 鐢ㄤ簬璇婃柇鐨勭畝鐭緭鍑烘憳瑕併€?
        error: 鍙€夌殑閿欒娑堟伅銆?
        created_at: 姝ラ鍒涘缓鏃剁殑鏃堕棿鎴炽€?
        updated_at: 姝ラ鏈€杩戞洿鏂版椂鐨勬椂闂存埑銆?

    杩斿洖:
        涓€涓楠ょ姸鎬佽褰曘€?

    寮傚父:
        鏃犮€?

    鍓綔鐢?
        鏃犮€?
    """

    step_id: str
    turn_id: str
    step_type: str
    status: str
    input_summary: str
    output_summary: str
    error: Optional[str]
    created_at: datetime
    updated_at: datetime
