"""全局指令（``AGENTS.md``）配置的 HTTP 路由。

负责 ``/configuration/global-instructions`` 的读取与保存：schema 转换、依赖装配与错误映射。
token 预算检查、路径边界与原子写入由 ``InstructionConfigurationService`` 承担，本模块不直接
读写文件。
"""

from __future__ import annotations

from app.api.configuration.errors import raise_configuration_error
from app.api.schemas.request.GlobalInstructionUpdateRequest import GlobalInstructionUpdateRequest
from app.api.schemas.response.GlobalInstructionResponse import GlobalInstructionResponse
from app.app import app
from app.service.configuration.instruction_configuration_service import (
    InstructionConfigurationService,
)
from app.service.configuration.system_prompt_update_broadcaster import (
    broadcast_system_prompt_delta,
    build_system_prompt_delta,
)
from app.task_runtime.system_prompt_delta_source import SystemPromptDeltaSource


@app.get("/configuration/global-instructions", response_model=GlobalInstructionResponse)
async def get_global_instruction_configuration() -> GlobalInstructionResponse:
    try:
        return GlobalInstructionResponse.from_document(InstructionConfigurationService().read())
    except Exception as exc:
        raise_configuration_error(exc)


@app.put("/configuration/global-instructions", response_model=GlobalInstructionResponse)
async def update_global_instruction_configuration(
    payload: GlobalInstructionUpdateRequest,
) -> GlobalInstructionResponse:
    try:
        service = InstructionConfigurationService()
        previous = service.read().content
        document = service.update(payload.content)
        delta = build_system_prompt_delta(
            source=SystemPromptDeltaSource.GLOBAL_INSTRUCTIONS,
            previous=previous,
            current=document.content,
        )
        if delta is not None:
            broadcast_system_prompt_delta(delta)
        return GlobalInstructionResponse.from_document(document)
    except Exception as exc:
        raise_configuration_error(exc)
