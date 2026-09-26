"""系统默认子 Agent JSON 的首次安装。

本模块只负责把随应用分发的默认配置复制到系统 `.cosir/agents` 目录，不解析、不校验
Agent 配置本身：JSON schema 校验与 profile 构造统一由
``app.core.agents.agent_profile.AgentProfile.vaild_agent_profile`` 持有。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app.core.agents.agent_profile import AgentProfileConfigError
from app.utils.cosir_paths import system_agent_config_dir

_DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
_DEFAULTS_MARKER = ".defaults-initialized"


def initialize_system_agent_defaults() -> Path:
    """将随应用分发的默认 JSON 安全导入系统配置目录。

    标记文件不存在时只补入缺少的默认文件，不覆盖已有用户文件；默认内容先写入同目录临时
    文件再原子替换。所有默认文件写入成功后才落初始化标记，因此中断后可以重试。初始化标记
    存在后不再自动恢复用户删除的默认 Agent。

    返回:
        系统级 Agent 配置目录。

    异常:
        AgentProfileConfigError: 默认资源缺失或无法复制、创建目录或写初始化标记。

    副作用:
        创建系统 `.cosir/agents` 目录，首次初始化时写入默认 JSON 和标记文件。
    """

    directory = system_agent_config_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / _DEFAULTS_MARKER
        if marker.exists():
            return directory
        default_files = sorted(_DEFAULTS_DIR.glob("*.json"))
        if not default_files:
            raise AgentProfileConfigError(f"默认 Agent JSON 资源为空: {_DEFAULTS_DIR}")
        for source in default_files:
            target = directory / source.name
            if target.exists():
                continue
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=directory, prefix=f".{source.name}.", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(source.read_bytes())
                temporary.flush()
                os.fsync(temporary.fileno())
            if target.exists():
                temporary_path.unlink(missing_ok=True)
            else:
                os.replace(temporary_path, target)
        marker_tmp = directory / f".{_DEFAULTS_MARKER}.tmp"
        marker_tmp.write_text("initialized\n", encoding="utf-8")
        os.replace(marker_tmp, marker)
    except AgentProfileConfigError:
        raise
    except OSError as exc:
        raise AgentProfileConfigError(
            f"系统 Agent 默认配置初始化失败，目录={directory}，原因={type(exc).__name__}: {exc}"
        ) from exc
    return directory
