"""``read_image`` 的展示数据投影：读到的图片（附件 asset id）与没读成的路径
及短提示。"""

from typing import Any


def build_read_image_display_data(
    images: list[dict[str, Any]], failures: list[dict[str, str]]
) -> dict[str, Any]:
    """``images`` 每项 ``{path, asset_id, width, height}``；``failures`` 每项 ``{path, hint}``。

    ``hint`` 是受控的中文短提示（来自工具里的错误码映射），不放原始异常。
    """

    return {"kind": "image-read", "images": images, "failures": failures}
