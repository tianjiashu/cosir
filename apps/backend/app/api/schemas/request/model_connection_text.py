"""连接字段的清洗规则：新建、更新连接与连接测试、模型目录发现共用。"""


def normalize_connection_text(value: str, field_name: str) -> str:
    """拒绝空白文本并去除首尾空白；Base URL 另去除末尾斜杠（服务层在其后拼接路径）。"""

    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized.rstrip("/") if field_name == "base_url" else normalized
