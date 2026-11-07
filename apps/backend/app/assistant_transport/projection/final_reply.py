"""最终回复是现有响应与 Run 结局的展示投影，不读取结构化支线的 final_output。"""

from app.config.constant import Constant


def is_final_reply(status: str, finish_reason: str | None, text: str) -> bool:
    """调用方先选定实际提交的末次无工具响应；截断、取消与空输出不构成最终回复。"""
    return (
        status == "completed"
        and finish_reason in Constant.Workflow.NORMAL_FINISH_REASONS
        and bool(text.strip())
    )
