"""共享 Skill 目录：Agent Skills 生态的公共位置 ``.agents/skills``（唯一事实源）。

系统级是 ``<用户主目录>/.agents/skills``（macOS、Linux 为 ``~/.agents/skills``，Windows 为
``%USERPROFILE%\\.agents\\skills``），workspace 是 ``<工作区根>/.agents/skills``。Codex、Cursor、Copilot、
Gemini CLI、OpenCode 等都读这两个位置；cosir 也读，但从不往里写（导入落在 cosir 专属的 ``.cosir/skills``，
见 ``system_cosir`` / ``workspace_cosir``），也不建软链（Windows 建软链要权限，进 git 的软链在 Windows 上会
检出成文本文件）。

系统级目录固定在用户主目录，**不随** ``CODING_AGENT_DATA_DIR`` 走：Linux 上系统 ``.cosir`` 在 Electron
应用数据目录里，共享目录若跟着它就和其他工具对不上了。代价是数据根隔离不到这里，测试与桌面 E2E 要
另外把用户主目录指到临时目录（后端测试改 :func:`user_home_dir`，桌面 E2E 给后端设 ``HOME`` /
``USERPROFILE``）。

本模块只做路径推导，不创建目录。
"""

from __future__ import annotations

from pathlib import Path

SHARED_SKILLS_DIR: Path = Path(".agents", "skills")


def user_home_dir() -> Path:
    """系统级共享 Skill 目录的根；单独成函数，测试把它指到临时目录以隔离开发者本机。"""

    return Path.home()


def system_shared_skills_dir() -> Path:
    """系统级共享 Skill 目录（``<用户主目录>/.agents/skills``）。"""

    return user_home_dir() / SHARED_SKILLS_DIR


def workspace_shared_skills_dir(workspace_root: str | Path) -> Path:
    """workspace 共享 Skill 目录（``<工作区根>/.agents/skills``）。"""

    return Path(workspace_root) / SHARED_SKILLS_DIR


def home_relative(path: Path) -> str:
    """给人看的路径：在用户主目录下的写成 ``~/…``，否则原样。"""

    try:
        return f"~/{path.relative_to(user_home_dir()).as_posix()}"
    except ValueError:
        return str(path)
