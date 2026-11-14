"""``present_files`` 的展示数据投影：Agent 声明的交付物文件。"""

from typing import Any


def build_deliverables_display_data(files: list[dict[str, Any]]) -> dict[str, Any]:
    """``files`` 每项 ``{path, absolute_path, caption}``。

    ``path`` 给界面显示与打开：工作区内是相对路径（文件面板按它打开），工作区外是绝对路径；
    ``absolute_path`` 只给宿主「在文件夹中显示」。只含通过校验的文件，没通过的只告诉模型。
    """

    return {"kind": "deliverables", "files": files}
