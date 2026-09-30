"""远端 OpenAI 兼容模型目录发现服务。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from app.config.constant import Constant
from app.config.logging.logger import log
from app.utils.http_proxy import build_proxy_async_client


class ModelDiscoveryService:
    """请求并归一化一个 OpenAI 兼容端点的模型目录。

    本服务只负责远端 ``GET <base_url>/models``、鉴权、超时和响应投影；不负责模型配置持久化、
    模型能力推断、连接测试或前端展示。任何网络、HTTP、JSON 或响应结构错误都会被转换为空目录，
    使目录发现成为配置表单的可选增强能力。
    """

    async def discover(self, *, base_url: str, api_key: str) -> list[str]:
        """返回远端可见的模型 ID，失败时返回空列表。

        参数:
            base_url: OpenAI 兼容 API 根地址，例如 ``https://api.example.com/v1``；末尾斜杠
                会被忽略，路径前缀会保留。
            api_key: 发送给远端的 Bearer API Key；不会写入日志或返回值。

        返回:
            按远端返回顺序去重后的非空模型 ID 列表；请求失败、响应不是标准模型目录或目录为空时
            返回空列表。

        异常:
            不向调用方抛出远端请求异常；仅调用任务被取消时保留 ``asyncio.CancelledError`` 语义。

        副作用:
            发起一次带系统代理的外部 HTTP 请求，并写入不含 API Key 和响应正文的结构化日志。
        """

        models_url = _build_models_url(base_url)
        if models_url is None:
            log.warning(
                "model_discovery_invalid_base_url",
                extra={"msg": "模型目录发现地址无效", "data": {"base_url_valid": False}},
            )
            return []

        try:
            async with build_proxy_async_client(
                timeout=Constant.LLM.MODEL_DISCOVERY_TIMEOUT_SECONDS
            ) as client:
                response = await client.get(
                    models_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            response.raise_for_status()
            model_ids = _extract_model_ids(response.json())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "model_discovery_failed",
                extra={
                    "msg": "模型目录发现失败",
                    "data": {
                        "models_url": models_url,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return []

        if not model_ids:
            log.warning(
                "model_discovery_empty",
                extra={"msg": "模型目录为空或响应结构无效", "data": {"models_url": models_url}},
            )
            return []

        log.info(
            "model_discovery_succeeded",
            extra={
                "msg": "模型目录发现成功",
                "data": {"models_url": models_url, "model_count": len(model_ids)},
            },
        )
        return model_ids


def _build_models_url(base_url: str) -> str | None:
    """在保留 Base URL 路径前缀的前提下构造 ``/models`` 地址。"""

    try:
        parsed = urlsplit(base_url.rstrip("/"))
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    path = f"{parsed.path.rstrip('/')}/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _extract_model_ids(payload: object) -> list[str]:
    """从标准目录响应中提取并稳定去重模型 ID。

    标准响应是 ``{"object": "list", "data": [{"id": "..."}]}``；不把供应商的其他字段
    猜测为模型名。
    """

    records: object = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(records, list):
        return []

    model_ids: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        model_id = record.get("id")
        if not isinstance(model_id, str):
            continue
        normalized = model_id.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            model_ids.append(normalized)
    return model_ids
