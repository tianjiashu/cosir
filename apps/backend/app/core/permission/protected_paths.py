"""受保护路径：Agent 的文件工具不得读写的 cosir 自身数据（唯一事实源）。

系统 ``.cosir`` 里有 ``.env`` 与 ``config/models.json``（模型 Key）、MCP 配置、业务库与日志，Agent
读到就等于拿到凭据与全部对话历史，写进去就能改 cosir 自己的配置。其中 ``skills/`` 例外：Skill 正文
过长时，``skill`` 工具会让模型按绝对路径用 ``read_file`` 读剩下的部分，所以只读放行。

规则按「路径越具体越优先」取最深的那条（设计稿 ``docs/plan/agent-permission-sandbox-design.md``
§3、§7 的规则模型）。根目录全部来自路径变量（``system_cosir_dir()`` 随 ``CODING_AGENT_DATA_DIR``
与测试 ``override`` 变化），每次 :meth:`ProtectedPaths.current` 现取，不写死、不缓存。

消费方：``PathResolver`` 的读入口（读一份文件、读一个范围）与写入口，以及搜索遍历的剪枝。
工作区 ``.cosir`` 不在这里：它可读（下载、超长输出日志），拒写由 ``PathResolver`` 的保留区规则负责。
终端、浏览器的 ``file://`` 不经这里，要等命令沙箱与浏览器权限（AGENTS 待办「安全与权限」）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.utils.path.system_cosir import system_cosir_dir, system_skills_dir
from app.utils.path.validation import is_within

ProtectedAccess = Literal["deny", "read"]
"""``deny``：禁读禁写；``read``：只读。"""


@dataclass(frozen=True)
class ProtectedRule:
    """一条受保护规则：``root``（真实路径）及其子树按 ``access`` 处理。"""

    root: Path
    access: ProtectedAccess


@dataclass(frozen=True)
class ProtectedPaths:
    """某一时刻的受保护规则快照；按调用现取，判断时不再读路径变量。"""

    rules: tuple[ProtectedRule, ...]

    @classmethod
    def current(cls) -> ProtectedPaths:
        """按当前数据根推导规则；根取真实路径，判断时不再逐次解析。"""

        def rule(root: Path, access: ProtectedAccess) -> ProtectedRule:
            return ProtectedRule(Path(os.path.realpath(root)), access)

        return cls((rule(system_cosir_dir(), "deny"), rule(system_skills_dir(), "read")))

    def access(self, path: str | Path) -> ProtectedAccess | None:
        """返回路径（跟随符号链接后）命中的最具体规则的访问级别；未受保护返回 ``None``。"""

        matched = [rule for rule in self.rules if is_within(path, rule.root)]
        if not matched:
            return None
        return max(matched, key=lambda rule: len(rule.root.parts)).access

    def denies_read(self, path: str | Path) -> bool:
        """路径是否禁读。"""

        return self.access(path) == "deny"

    def denies_write(self, path: str | Path) -> bool:
        """路径是否禁写（受保护的路径一律不可写）。"""

        return self.access(path) is not None

    def is_denied_root(self, directory: Path) -> bool:
        """``directory`` 是否正是一条禁读规则的根，供遍历剪枝。

        遍历从已解析的真实路径出发、不跟随符号链接，子目录路径因此都是真实路径，按字面比较即可，
        不必每个目录都 ``realpath``。剪掉禁读根后，其下的只读例外（``skills/``）不会被遍历到，
        要搜 Skill 就以 Skill 目录为起点。
        """

        candidate = os.path.normcase(str(directory))
        return any(
            rule.access == "deny" and candidate == os.path.normcase(str(rule.root))
            for rule in self.rules
        )

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
            f"{system_skills_dir()} are readable."
        )
