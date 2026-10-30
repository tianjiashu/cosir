"""目录能力测试事实；不依赖网络或本机账号。"""

from app.core.llm_provider.claude.catalog import ClaudeModelInfo


def model_data(
    name="claude-sonnet-4-6", *, enabled=True, adaptive=True, disabled=True, effort=True
):
    return {
        "id": name,
        "display_name": name,
        "max_input_tokens": 1_000_000,
        "max_tokens": 128_000,
        "capabilities": {
            "thinking": {
                "supported": enabled or adaptive,
                "types": {
                    "enabled": {"supported": enabled},
                    "adaptive": {"supported": adaptive},
                    "disabled": {"supported": disabled},
                },
            },
            "effort": {
                "supported": effort,
                "low": {"supported": effort},
                "high": {"supported": effort},
                "max": {"supported": False},
            },
            "image_input": {"supported": True},
        },
    }


def sonnet_info():
    return ClaudeModelInfo.model_validate(model_data())
