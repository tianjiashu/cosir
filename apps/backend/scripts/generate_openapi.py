"""导出 FastAPI 应用的 OpenAPI schema 为静态 JSON 资产。

本脚本在后端装配完成后，从 ``app.api.app`` 取出模块级 FastAPI 单例，调用其
``openapi()`` 方法生成与代码实时同步的 OpenAPI 文档，落盘到仓库级
``docs/api/openapi.json``。前端同学可离线查阅，亦可喂给 ``openapi-typescript``
等工具自动生成 TS 客户端类型，直接 ``import`` 接口契约，避免手工对齐漂移。

用法::

    # 在 apps/backend 下执行（脚本会自动把 backend 根加入 sys.path）
    uv run python scripts/generate_openapi.py
    # 自定义输出路径
    uv run python scripts/generate_openapi.py --out path/to/openapi.json

注意：本脚本仅导入 ``app.api.app`` 触发路由注册，不启动 HTTP 服务、不执行
``lifespan``，因此不会触碰运行时资源（runtime / 存储 / 工具系统）。
"""

import argparse
import json
import sys
from pathlib import Path

# 将 backend 根目录加入导入路径，使 ``from app...`` 绝对导入可用，
# 无论调用方的工作目录在何处。
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 仓库根 = apps/backend 的上两级（apps -> coding-agent）。
REPO_ROOT = BACKEND_ROOT.parents[1]
DEFAULT_OUT = REPO_ROOT / "apps" / "shared" / "fastapi_docs.json"


def export_openapi(out_path: Path) -> Path:
    """生成并落盘 OpenAPI schema。

    参数:
        out_path: 输出 JSON 文件路径；父目录不存在时自动创建。

    返回:
        实际写入的文件路径。

    异常:
        RuntimeError: 当 FastAPI 应用装配或 schema 生成失败时抛出（原因为底层异常）。

    副作用:
        在 ``out_path`` 写入 OpenAPI JSON（UTF-8、缩进 2、保留非 ASCII）。
    """

    # 预热日志门面包以打破 ``settings`` <-> ``storage`` 的循环导入：``settings`` 模块顶层
    # ``from app.config.logging.common import current_log_file`` 会触发 ``logging`` 包
    # ``__init__``，进而经 ``configuration -> sqlite_handler -> store_engines -> settings``
    # 回引自身。只有当 ``logging`` 包先于 ``settings`` 被导入、且 ``common`` 子模块已就绪时，
    # 回引的 ``settings`` 才能顺利完成（与 ``app.__main__`` 入口先 import logging 的顺序一致，
    # 也与 ``runner.py`` 首行 ``from app.config.logging import ...`` 的导入顺序一致）。

    try:
        from app.app import app
    except Exception as exc:
        raise RuntimeError("无法导入 FastAPI 应用，OpenAPI 导出中止") from exc

    try:
        schema = app.openapi()
    except Exception as exc:
        raise RuntimeError("OpenAPI schema 生成失败") from exc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def main() -> None:
    """解析命令行参数并执行导出。"""

    parser = argparse.ArgumentParser(description="导出 FastAPI OpenAPI schema 为静态 JSON。")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"输出路径（默认：{DEFAULT_OUT}）",
    )
    args = parser.parse_args()

    written = export_openapi(args.out)
    print(f"已导出 OpenAPI 文档 -> {written}")


if __name__ == "__main__":
    main()
