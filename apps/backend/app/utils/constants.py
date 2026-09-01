"""跨层共享的纯常量事实源（leaf 层）。

单一职责：承载项目级、与具体业务模型无关、但被多个分层（api / core / service /
models / utils）共同引用的基础常量。放在最底层 leaf 模块，避免上层互相引用造成的
循环依赖（如 ``file_utils`` 与 ``models.attachment_ref`` 互引）。

图片判定以标准库 ``mimetypes`` 为优先事实源（见 ``app.utils.image_utils.is_image_path``）；
本模块的 ``IMAGE_EXTENSIONS`` 是**标准库未覆盖时的业务补充白名单**，仅保留标准库在
部分运行平台/注册表缺失时可能漏判、但本项目视觉通道需要强制路由的扩展名。任何需要
"标准库兜底集合"的地方引用此常量，不得就地定义字面量集合，确保补充后缀只有一份、不会漂移。
"""

from __future__ import annotations

# 标准库 mimetypes 覆盖不到、但本项目视觉通道需强制路由的图片扩展名（小写、含点）。
# 注意：DeepSeek 官方视觉文档不支持 bmp，但此处保留 .bmp 是为了把该类文件**路由到**
# 运行期视觉通道（build_user_content_blocks 会做格式/体积校验并拒绝），而非静默当成
# 普通文件拼进文本——属于"早fail"而非"错误归类"。
IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
