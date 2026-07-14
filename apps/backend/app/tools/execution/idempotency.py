"""工具调用幂等键构造。"""

from hashlib import sha256
import json
from typing import Any, Mapping, Optional


class ToolIdempotencyKeyBuilder:
    """为工具调用生成稳定且可重试的幂等键。"""

    def build(
        self,
        run_id: str,
        step_id: Optional[str],
        tool_name: str,
        arguments: Mapping[str, Any],
        approval_id: Optional[str] = None,
    ) -> str:
        """基于调用语义生成 SHA-256 幂等键。

        参数:
            run_id: 所属 Durable Run 标识；兼容调用可为空字符串。
            step_id: 可选步骤标识。
            tool_name: 稳定工具名。
            arguments: 已通过 schema 校验的工具参数。
            approval_id: 可选审批标识；获批后的调用与初次请求保持关联。

        返回:
            带 ``tool:`` 前缀的稳定幂等键。

        异常:
            TypeError: 当 arguments 含不可 JSON 序列化的值时抛出。

        副作用:
            无。
        """

        payload = {
            "approval_id": approval_id or "",
            "arguments": arguments,
            "run_id": run_id,
            "step_id": step_id or "",
            "tool_name": tool_name,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return f"tool:{sha256(encoded.encode('utf-8')).hexdigest()}"
