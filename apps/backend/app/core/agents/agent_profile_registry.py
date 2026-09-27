"""进程内按配置作用域索引和解析 Agent profile。

单一职责：按「作用域 + ``agent_id``」在内存中注册、解析与列举 Agent profile。
不负责：配置文件读取与 JSON 字段校验（见 ``AgentProfile.vaild_agent_profile``）、磁盘持久化、
Run / context 运行态，以及工具实现。

作用域键：系统内置 profile 使用哨兵 ``AgentProfileRegistry.SYSTEM_WORKSPACE``；workspace
profile 使用其根路径经 ``normalize_workspace`` 规范化后的字符串，同一 workspace 的不同写法
（大小写、相对路径）必须归一到同一键。

日志：配置无效的文件、重复或冲突的注册、整个作用域加载失败都会落盘，便于启动后复盘
「为什么某个子 Agent 不见了」；日志是诊断旁路，写失败不影响索引结果。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from app.config.logging.logger import log
from app.core.agents.agent_profile import (
    AgentProfile,
    AgentProfileConfigError,
    AgentProfileType,
)


class AgentProfileRegistry:
    """在内存中统一保存系统级及各 workspace 的 Agent profile。

    键为 ``(作用域, agent_id)``。系统内置 profile 由启动装配注册
    （``app.config.configuration.build_agent_registry``），workspace profile 由
    :meth:`load_agent_profiles` 从各自 ``.cosir/agents`` 目录加载。

    冲突与重复遵循「先注册者优先、system 基线不被 workspace 覆盖」：同一作用域重复注册、
    或跨作用域 ``agent_id`` 冲突都不覆盖已有 profile，只跳过并写 warning 日志。单个 JSON
    文件的配置无效更轻：该文件由 ``AgentProfile.vaild_agent_profile`` 记 error 日志后返回
    ``None``，此处跳过，整批加载继续。只有目录级问题才向上抛
    ``AgentProfileConfigError``。

    并发契约：
        - 索引由一把可重入锁（``threading.RLock``）统一保护：写（:meth:`register` /
          :meth:`replace` / :meth:`unregister` / :meth:`load_agent_profiles`）与读
          （:meth:`resolve` / :meth:`list` / :meth:`list_system_sub_agents` 等）都在同一临界区
          内直接读写 ``self._profiles``，同一时刻只有一个线程处于临界区；
        - 必须可重入：部分读方法（:meth:`list_agent_ids` / :meth:`child_agent_summary` /
          :meth:`child_agent_ids`）在自身临界区内复用 :meth:`list`；
        - 因此读路径不会与并发写入交错，也不会抛
          ``RuntimeError: dictionary changed size during iteration``；
        - :meth:`load_agent_profiles` 整批生效：逐文件读取在锁外完成，合并全程持锁，读者只
          可能看到「加载前」或「加载后」，不会看到半装填的作用域；
        - 临界区内只做内存字典操作：不做 IO、不调用外部代码、不写日志（日志一律在出锁后记
          录），避免把锁的生命周期交给日志实现；
        - ``AgentProfile`` 必须视为不可变值对象：读路径返回的是索引中的共享实例，调用方不得
          就地修改其字段，需要变更时用 :meth:`replace` 整体替换。

    已知缺口（历史 docstring 曾声明、当前实现尚未提供）：
        - 未校验配置文件名与 ``agent_id`` 是否一致；
        - 未记录「加载失败的作用域」，因此 :meth:`resolve` / :meth:`list` 等不会因 workspace
          配置无效而抛 ``AgentProfileConfigError``，实际表现为该 workspace 未命中即回退
          system 作用域。
    """

    SYSTEM_WORKSPACE = "system"

    def __init__(self) -> None:
        # 一把可重入锁保护索引的全部读写：临界区内只做内存字典操作，不 IO、不写日志，
        # 因此不存在嵌套锁顺序问题。可重入是必需的——部分读方法在临界区内复用 list()。
        self._lock = threading.RLock()
        self._profiles: dict[tuple[str, str], AgentProfile] = {}

    @classmethod
    def normalize_workspace(cls, workspace: str | Path) -> str:
        """将 workspace 根路径归一化为 Registry 作用域键。

        参数:
            workspace: 系统作用域哨兵或 workspace 根路径。

        返回:
            系统哨兵 ``system`` 或绝对、规范化后的 workspace 路径。

        异常:
            ValueError: workspace 路径为空。

        副作用:
            无；只规范化路径字符串，不读取 Agent 配置文件。
        """

        if str(workspace) == cls.SYSTEM_WORKSPACE:
            return cls.SYSTEM_WORKSPACE
        text = str(workspace).strip()
        if not text:
            raise ValueError("workspace path must not be empty")
        return os.path.normcase(os.path.abspath(os.path.normpath(text)))

    @staticmethod
    def _decide_registration(
        current: dict[tuple[str, str], AgentProfile],
        scope: str,
        profile: AgentProfile,
    ) -> tuple[bool, str | None]:
        """判定一份 profile 能否进入指定作用域（纯函数，不加锁、不写日志）。

        判定规则与历史实现一致：同一作用域已存在同 ``agent_id`` 视为重复；``agent_id`` 与
        另一作用域冲突时保留先注册者（system 基线不被 workspace 覆盖）。单条注册与整批加载
        共用本函数，避免两处规则漂移。

        参数:
            current: 判定所依据的索引；调用方必须已持有 :attr:`_lock`，判定期间不会有并发修改。
            scope: 经 ``normalize_workspace`` 归一化后的作用域键。
            profile: 待注册的 profile。

        返回:
            ``(accepted, conflict_scope)``：``accepted`` 为 ``False`` 表示同作用域重复（此时
            ``conflict_scope`` 恒为 ``None``）；``conflict_scope`` 非空表示与另一作用域冲突，
            取先注册者的作用域键。

        异常:
            无。

        副作用:
            无；只读入参索引。
        """

        if (scope, profile.agent_id) in current:
            return False, None
        if scope == AgentProfileRegistry.SYSTEM_WORKSPACE:
            return True, next(
                (
                    registered_scope
                    for registered_scope, registered_id in current
                    if registered_id == profile.agent_id
                ),
                None,
            )
        if (AgentProfileRegistry.SYSTEM_WORKSPACE, profile.agent_id) in current:
            return True, AgentProfileRegistry.SYSTEM_WORKSPACE
        return True, None

    def register(self, workspace: str | Path, profile: AgentProfile) -> bool:
        """将内存中的 profile 注册到指定作用域，重复或冲突时跳过并告警。

        本方法是「是否接受一份 profile」的判定入口（规则见 :meth:`_decide_registration`）：
        同一作用域重复、workspace 与 system 的 ``agent_id`` 冲突都在这里裁决，调用方无需再
        预筛。判定与写入在同一临界区内完成（原地写入索引），因此与并发的 :meth:`list` /
        :meth:`resolve` 交错时不会出现覆盖写或迭代异常。

        参数:
            workspace: 系统作用域哨兵或 workspace 根路径。
            profile: 已构造并校验的 Agent profile。

        返回:
            成功注册返回 ``True``；重复或冲突时返回 ``False``。

        异常:
            无。重复与冲突都不抛错，以免单个冲突中断整批加载。

        副作用:
            持 :attr:`_lock` 原地写入当前进程内存索引；跳过时写 warning 日志（出锁后记录）——
            ``agent_profile_duplicate_skipped``（同作用域重复）或
            ``agent_profile_conflict_skipped``（跨作用域冲突），两者都带 ``scope`` 与
            ``agent_id``，冲突时额外带 ``conflict_scope``。不读写文件或数据库。
        """

        scope = self.normalize_workspace(workspace)
        accepted = False
        conflict: str | None = None
        with self._lock:
            accepted, conflict = self._decide_registration(self._profiles, scope, profile)
            if accepted and conflict is None:
                self._profiles[(scope, profile.agent_id)] = profile
        if not accepted:
            log.warning(
                "agent_profile_duplicate_skipped",
                extra={
                    "msg": "同一作用域内 Agent ID 重复，保留先注册的 profile",
                    "data": {"scope": scope, "agent_id": profile.agent_id},
                },
            )
            return False
        if conflict is not None:
            log.warning(
                "agent_profile_conflict_skipped",
                extra={
                    "msg": "Agent ID 与另一作用域冲突，保留先注册的 profile",
                    "data": {
                        "scope": scope,
                        "agent_id": profile.agent_id,
                        "conflict_scope": conflict,
                    },
                },
            )
            return False
        return True

    def replace(self, workspace: str | Path, profile: AgentProfile) -> AgentProfile:
        """替换指定作用域中已注册的 profile。

        参数:
            workspace: 系统哨兵或 workspace 根路径。
            profile: 已通过配置校验的新 profile。

        返回:
            被替换的旧 profile，便于调用方记录或回滚。

        异常:
            KeyError: 该作用域尚未注册对应 ``agent_id``。

        副作用:
            持 :attr:`_lock` 原地更新当前进程内存索引，不读写配置文件。
        """

        scope = self.normalize_workspace(workspace)
        key = (scope, profile.agent_id)
        with self._lock:
            previous = self._profiles.get(key)
            if previous is None:
                raise KeyError(profile.agent_id)
            self._profiles[key] = profile
        return previous

    def unregister(self, workspace: str | Path, agent_id: str) -> AgentProfile:
        """卸载指定作用域中的 profile。

        参数:
            workspace: 系统哨兵或 workspace 根路径。
            agent_id: 要卸载的 Agent 标识。

        返回:
            被卸载的 profile。

        异常:
            KeyError: 该作用域未注册对应 Agent。

        副作用:
            持 :attr:`_lock` 从当前进程内存索引原地删除 profile，不读写配置文件。
        """

        scope = self.normalize_workspace(workspace)
        with self._lock:
            try:
                removed = self._profiles.pop((scope, agent_id))
            except KeyError as exc:
                raise KeyError(agent_id) from exc
        return removed

    def load_agent_profiles(self, workspace: str | Path, directory: str | Path) -> None:
        """从指定作用域的配置目录加载全部 JSON profile 到内存。

        单个文件无效不会中断加载（该文件已由 ``AgentProfile.vaild_agent_profile`` 记日志并
        跳过）；重复与冲突由 :meth:`register` 统一裁决并告警。目录级失败才向上抛出，由启动
        编排按作用域决定处置方式。

        参数:
            workspace: 系统哨兵 ``system`` 或 workspace 根路径。
            directory: 对应作用域的 ``.cosir/agents`` 配置目录。

        返回:
            无。

        异常:
            AgentProfileConfigError: 配置目录是失效符号链接、目录本身是符号链接、路径不是
                目录，或目录读取失败时抛出；抛出前写 error 日志
                （``agent_profile_scope_load_failed``，含作用域、目录、异常类型与消息）。

        副作用:
            读取配置目录；成功时**整批**更新该作用域的内存索引（文件读取在锁外、合并全程持锁，
            读者不会看到半装填作用域），失败时写 error 日志后向上抛出且索引保持原样。
            不修改磁盘内容，也不清理该作用域此前的加载结果。
        """

        scope = self.normalize_workspace(workspace)
        try:
            # 文件读取可能较慢也可能失败，放在锁外完成；失败时索引保持原样。
            profiles = self._load_agent_profiles(directory)
        except AgentProfileConfigError as exc:
            log.error(
                "agent_profile_scope_load_failed",
                extra={
                    "msg": "Agent 配置目录加载失败",
                    "data": {
                        "scope": scope,
                        "directory": str(directory),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                },
            )
            raise
        skipped_duplicates: list[str] = []
        skipped_conflicts: list[tuple[str, str]] = []
        with self._lock:
            for profile in profiles:
                accepted, conflict = self._decide_registration(self._profiles, scope, profile)
                if not accepted:
                    skipped_duplicates.append(profile.agent_id)
                    continue
                if conflict is not None:
                    skipped_conflicts.append((profile.agent_id, conflict))
                    continue
                self._profiles[(scope, profile.agent_id)] = profile
        # 日志出锁后再记：日志是 IO，不应占着锁；事件名与 data 结构同 :meth:`register`。
        for agent_id in skipped_duplicates:
            log.warning(
                "agent_profile_duplicate_skipped",
                extra={
                    "msg": "同一作用域内 Agent ID 重复，保留先注册的 profile",
                    "data": {"scope": scope, "agent_id": agent_id},
                },
            )
        for agent_id, conflict_scope in skipped_conflicts:
            log.warning(
                "agent_profile_conflict_skipped",
                extra={
                    "msg": "Agent ID 与另一作用域冲突，保留先注册的 profile",
                    "data": {
                        "scope": scope,
                        "agent_id": agent_id,
                        "conflict_scope": conflict_scope,
                    },
                },
            )

    def resolve(self, workspace: str | Path, agent_id: str) -> AgentProfile | None:
        """按 workspace 优先、system 回退的规则解析 profile。

        参数:
            workspace: 当前 workspace 根路径或系统哨兵。
            agent_id: 要解析的 Agent ID。

        返回:
            workspace 或 system 中匹配的 profile；未找到时返回 ``None``。

        异常:
            无。workspace 未注册或配置无效时都不抛错，只按当前内存索引给出结果（见类
            docstring 的已知缺口）。

        副作用:
            无；仅读取内存索引。
        """

        scope = self.normalize_workspace(workspace)
        # 两次查询在同一临界区内完成，不会读到跨写入的混合视图（workspace 未命中才回退 system）。
        with self._lock:
            return self._profiles.get((scope, agent_id)) or self._profiles.get(
                (self.SYSTEM_WORKSPACE, agent_id)
            )

    def list(self, workspace: str | Path) -> list[AgentProfile]:
        """列出指定 workspace 可见的 profile；系统作用域 profile 排在 workspace profile 前。

        参数:
            workspace: 当前 workspace 根路径或系统哨兵。

        返回:
            system 与当前 workspace 作用域内的 profile 列表，不包含其他 workspace。

        异常:
            无。workspace 未注册或配置无效时都不抛错，只按当前内存索引给出结果（见类
            docstring 的已知缺口）。

        副作用:
            无；仅读取内存索引。
        """

        scope = self.normalize_workspace(workspace)
        # 遍历在临界区内完成，与并发写入互斥，因此不会出现迭代异常或半装填作用域。
        with self._lock:
            profiles = [
                profile
                for (profile_scope, _), profile in self._profiles.items()
                if profile_scope == self.SYSTEM_WORKSPACE
            ]
            if scope != self.SYSTEM_WORKSPACE:
                profiles.extend(
                    profile
                    for (profile_scope, _), profile in self._profiles.items()
                    if profile_scope == scope
                )
        return profiles

    def list_agent_ids(self, workspace: str | Path) -> set[str]:
        """列出指定 workspace 可见的所有 Agent ID。

        参数:
            workspace: 当前 workspace 根路径或系统哨兵。

        返回:
            system 与当前 workspace 可见 profile 的 ID 集合。

        异常:
            无。workspace 未注册或配置无效时都不抛错，只按当前内存索引给出结果（见类
            docstring 的已知缺口）。

        副作用:
            无；仅读取内存索引。
        """

        return {profile.agent_id for profile in self.list(workspace)}

    def child_agent_summary(self, workspace: str | Path) -> str:
        """将指定 workspace 可见的 CHILD profile 投影为委派能力摘要。

        参数:
            workspace: 当前 workspace 根路径或系统哨兵。

        返回:
            可供父 Agent 读取的子 Agent ID 与 description 摘要；没有 CHILD 时为空字符串。

        异常:
            无。workspace 未注册或配置无效时都不抛错，只按当前内存索引给出结果（见类
            docstring 的已知缺口）。

        副作用:
            无；仅读取内存索引，不包含系统提示词正文。
        """

        blocks = [
            f"agent_id: {profile.agent_id} | description: {profile.description or ''}"
            for profile in self.list(workspace)
            if profile.agent_type is AgentProfileType.CHILD
        ]
        if not blocks:
            return ""
        return "Available child agents:\n" + "\n".join(blocks)

    def child_agent_ids(self, workspace: str | Path) -> set[str]:
        """列出指定 workspace 可见的 CHILD Agent ID。

        参数:
            workspace: 当前 workspace 根路径或系统哨兵。

        返回:
            system 与当前 workspace 可见的 CHILD ID 集合。

        异常:
            无。workspace 未注册或配置无效时都不抛错，只按当前内存索引给出结果（见类
            docstring 的已知缺口）。

        副作用:
            无；仅读取内存索引。
        """

        return {
            profile.agent_id
            for profile in self.list(workspace)
            if profile.agent_type is AgentProfileType.CHILD
        }

    def list_system_sub_agents(self) -> list[AgentProfile]:
        """列出系统作用域内已注册的 CHILD profile，不包含主 Agent。"""

        with self._lock:
            return [
                profile
                for (profile_scope, _), profile in self._profiles.items()
                if profile_scope == self.SYSTEM_WORKSPACE
                and profile.agent_type is AgentProfileType.CHILD
            ]

    def list_workspace_sub_agents(self, workspace: str | Path) -> list[AgentProfile]:
        """列出指定 workspace 作用域内的 Agent profile。

        参数：
            workspace: 当前 workspace 根路径或系统哨兵。

        返回：
            当前作用域内的 profile 列表；调用方获得的是 Registry 中的共享不可变值对象。

        异常：
            ValueError: workspace 路径为空时由 ``normalize_workspace`` 抛出。

        副作用：
            无；仅读取受锁保护的进程内索引。
        """
        scope = self.normalize_workspace(workspace)
        with self._lock:
            return [
                profile
                for (profile_scope, _), profile in self._profiles.items()
                if profile_scope == scope
            ]

    def _load_agent_profiles(self, directory: str | Path) -> list[AgentProfile]:
        """严格读取目录直接子项 JSON 文件，且只接受目录内普通文件。

        参数:
            directory: system 或 workspace 对应的 ``.cosir/agents`` 配置目录。

        返回:
            按文件名排序、逐个通过 ``AgentProfile.vaild_agent_profile`` 的 profile 列表。
            配置无效的文件由该方法记 error 日志后返回 ``None``，此处跳过、不进入列表（该
            跳过因此不需要重复记日志）。

        异常:
            AgentProfileConfigError: 目录是失效符号链接、目录本身是符号链接、路径不是目录、
                配置文件符号链接越出目录，或目录读取失败（``OSError`` / ``RuntimeError`` /
                ``UnicodeDecodeError`` 统一包装为本异常）。

        副作用:
            读取配置目录及文件；不修改磁盘内容。
        """

        root = Path(directory)
        try:
            if not root.exists():
                if root.is_symlink():
                    raise AgentProfileConfigError(f"Agent 配置目录是失效符号链接: {root}")
                return []
            if root.is_symlink():
                raise AgentProfileConfigError(f"Agent 配置目录不能是符号链接: {root}")
            resolved_root = root.resolve(strict=True)
            if not resolved_root.is_dir():
                raise AgentProfileConfigError(f"Agent 配置路径不是目录: {root}")

            profiles: list[AgentProfile] = []
            for path in sorted(root.glob("*.json"), key=lambda item: item.name.casefold()):
                resolved_path = path.resolve(strict=True)
                if not resolved_path.is_relative_to(resolved_root):
                    raise AgentProfileConfigError(f"配置文件符号链接越出 Agent 目录: {path}")
                profile = AgentProfile.vaild_agent_profile(path)
                if profile is None:
                    continue
                profiles.append(profile)
            return profiles
        except AgentProfileConfigError:
            raise
        except (OSError, RuntimeError, UnicodeDecodeError) as exc:
            raise AgentProfileConfigError(
                f"读取 Agent 配置目录失败，目录={root}，原因={type(exc).__name__}: {exc}"
            ) from exc
