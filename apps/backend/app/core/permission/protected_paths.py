"""受保护路径：Agent 的文件工具不得读写的 cosir 自身数据（唯一事实源）。

系统 ``.cosir`` 里有 ``.env`` 与 ``config/models.json``（模型 Key）、MCP 配置、业务库与日志，Agent
读到就等于拿到凭据与全部对话历史，写进去就能改 cosir 自己的配置，所以整棵子树禁读禁写。其中 cosir 专属
Skill 目录 ``skills/`` 例外、只读放行：Skill 正文过长时，``skill`` 工具会让模型按绝对路径用 ``read_file``
读剩下的部分；不放行写，Agent 改不了 cosir 专属 Skill（以后的内置 Skill 也在这一侧）。

根目录来自路径变量（``system_cosir_dir()`` 随 ``CODING_AGENT_DATA_DIR`` 与测试 ``override`` 变化），每次
:meth:`ProtectedPaths.current` 现取，不写死、不缓存。权限模型扩展为多条规则时见设计稿
``docs/plan/agent-permission-sandbox-design.md``。

消费方：``PathResolver`` 的读入口（读一份文件、读一个范围）与写入口，搜索遍历的剪枝，以及 Skill 文件
读取（外部软链可能指进这里）。工作区 ``.cosir`` 不在这里：它可读（下载、超长输出日志），拒写由
``PathResolver`` 的保留区规则负责。终端、浏览器的 ``file://`` 不经这里，要等命令沙箱与浏览器权限
（AGENTS 待办「安全与权限」）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.utils.path.system_cosir import system_cosir_dir, system_cosir_skills_dir
from app.utils.path.validation import is_within


@dataclass(frozen=True)
class ProtectedPaths:
    """某一时刻的受保护根与只读例外（都是真实路径）；按调用现取，判断时不再读路径变量。"""

    root: Path
    readable: Path

    @classmethod
    def current(cls) -> ProtectedPaths:
        """按当前数据根推导；两者都取真实路径，判断时不再逐次解析。"""

        return cls(
            Path(os.path.realpath(system_cosir_dir())),
            Path(os.path.realpath(system_cosir_skills_dir())),
        )

    def denies_read(self, path: str | Path) -> bool:
        """路径（跟随符号链接后）是否禁读：在受保护子树内，且不在 Skill 只读例外里。"""

        return is_within(path, self.root) and not is_within(path, self.readable)

    def denies_write(self, path: str | Path) -> bool:
        """路径是否禁写：受保护子树内一律不可写，Skill 目录也不例外。"""

        return is_within(path, self.root)

    def is_denied_root(self, directory: Path) -> bool:
        """``directory`` 是否正是受保护根，供遍历剪枝。

        遍历从已解析的真实路径出发、不跟随符号链接，子目录路径因此都是真实路径，按字面比较即可，
        不必每个目录都 ``realpath``。剪掉受保护根后其下的 ``skills/`` 不会被遍历到，要搜 Skill 就以
        Skill 目录为起点。
        """

        return os.path.normcase(str(directory)) == os.path.normcase(str(self.root))

    def denial_message(self, path: str, *, write: bool) -> str:
        """面向模型的拒绝说明。"""

        root = system_cosir_dir()
        if write:
            return (
                f"path is inside cosir's own data directory ({root}): {path}. Files there "
                "cannot be modified by tools."
            )
        return (
            f"path is inside cosir's own data directory ({root}): {path}. It holds settings, "
            f"API keys and conversation history and cannot be read; only skills under "
            f"{system_cosir_skills_dir()} are readable."
        )
