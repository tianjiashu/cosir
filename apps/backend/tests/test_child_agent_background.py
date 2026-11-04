"""后台工具参数的契约：委派始终登记工作，消息发送始终普通返回。"""

import pytest

from app.core.tool.multi_agent import send_message


@pytest.mark.usefixtures("isolated_storage")
def test_delegation_and_send_do_not_expose_a_background_switch() -> None:
    """委派不要求模式开关，普通消息也不能要求登记等待。"""
    from app.core.tool.multi_agent.args.delegate_task_args import DelegateTaskArgs
    from app.core.tool.multi_agent.args.send_message_args import SendMessageArgs

    assert "background" not in DelegateTaskArgs.model_fields
    assert "background" not in SendMessageArgs.model_fields
    assert send_message.SendMessageTool().to_definition().background is None
