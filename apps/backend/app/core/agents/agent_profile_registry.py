"""进程内按配置作用域索引和解析 Agent profile。

单一职责：按「作用域 + ``agent_id``」在内存中注册、解析与列举 Agent profile，并在某个作用域
**首次被读取时**把该作用域的配置文件装载进来。

不负责：配置文件读取与 JSON 字段校验（见 ``AgentProfileScopeSource`` 与
``AgentProfile.vaild_agent_profile``）、磁盘持久化、Run / context 运行态，以及工具实现。

作用域键：系统内置 profile 使用哨兵 ``AgentProfileRegistry.SYSTEM_WORKSPACE``；workspace
profile 使用其根路径经 ``normalize_workspace`` 规范化后的字符串（口径见
``app.utils.workspace_scope``），同一 workspace 的不同写法（大小写、相对路径）必须归一到同一键。

为什么懒装载：workspace 可以在进程运行期间创建，若只在启动期遍历一次已登记 workspace，
「启动之后才创建的 workspace」的配置在整个进程周期内都不可见；而把装载分散到各调用点后，
任何新增读取路径都会再次漏掉——装载收口在索引读原语内部，调用方无需也不应显式装载。

日志：装载成功、降级与冲突都落盘，便于运行后复盘「为什么某个子 Agent 不见了」；日志是诊断
旁路，写失败不影响索引结果。
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path

from app.config.logging.logger import log
from app.core.agents.agent_profile import (
    AgentProfile,
    AgentProfileType,
)
from app.core.agents.agent_profile_source import AgentProfileScopeSource
from app.utils.workspace_scope import SYSTEM_SCOPE as _SYSTEM_SCOPE
from app.utils.workspace_scope import normalize_scope_path


class AgentProfileRegistry:
    """在内存中统一保存系统级及各 workspace 的 Agent profile，并按需装载作用域。

    键为 ``(作用域, agent_id)``。系统内置 profile 由启动装配注册
    （``app.config.configuration.build_agent_registry``）；用户 JSON 由装配期注入的
    :class:`AgentProfileScopeSource` 在作用域首次被读取时装载一次。

    冲突与重复遵循「先注册者优先、system 基线不被 workspace 覆盖」：同一作用域重复注册、
    或跨作用域 ``agent_id`` 冲突都不覆盖已有 profile，只跳过并写 warning 日志。装载沿用同一
    裁决，因此「workspace 用同名 agent_id 覆盖内置 profile」不会因懒装载而生效。

    为保证上面的「system 优先」不因懒装载退化成「哪个作用域先被读到谁赢」，任何行经
    workspace 作用域的注册与装载都**先确保 system 基线已装载**（见 :meth:`_ensure_scope`
    与 :meth:`register`）：system 的用户配置文件因此总能在跨作用域同名冲突中胜出，
    与启动期一次性装载时的顺序一致。

    失败策略：坏配置不影响运行。目录级问题与单文件无效都由 source 记日志后降级为空/跳过，
    读取路径不抛异常；该作用域仍被标记为「已装载」，避免每次读取重复读盘与刷日志。

    已知缺口：未校验配置文件名与 ``agent_id`` 是否一致——文件名只用于排序与日志定位，
    以 ``b.json`` 声明 ``agent_id: a`` 同样会被装载为 ``a``。

    并发契约：
        - 索引由一把可重入锁（``threading.RLock``）统一保护：写（:meth:`register` /
          :meth:`replace` / :meth:`unregister` / 装载合并）与读（:meth:`resolve` /
          :meth:`list` / :meth:`list_system_sub_agents` 等）都在同一临界区内直接读写
          ``self._profiles``，同一时刻只有一个线程处于临界区；
        - 必须可重入：部分读方法（:meth:`list_agent_ids` / :meth:`child_agent_summary` /
          :meth:`child_agent_ids`）在自身临界区内复用 :meth:`list`；
        - 因此读路径不会与并发写入交错，也不会抛
          ``RuntimeError: dictionary changed size during iteration``；
        - 装载由 :attr:`_load_lock` 与已装载集合串行化：并发首次读取同一作用域只读盘一次，
          读盘全程**不持有索引锁**；
        - 锁序恒为「装载锁 → 索引锁」：任何路径都不得在持有索引锁时申请装载锁；
        - 临界区内只做内存字典操作：不做 IO、不调用外部代码、不写日志（日志一律在出锁后记
          录），避免把锁的生命周期交给日志实现；
        - ``AgentProfile`` 必须视为不可变值对象：读路径返回的是索引中的共享实例，调用方不得
          就地修改其字段，需要变更时用 :meth:`replace` 整体替换。
    """

    # 哨兵值来自 app.utils.workspace_scope 的唯一来源，避免与 Team 配置注册表各写一份。
    SYSTEM_WORKSPACE = _SYSTEM_SCOPE

    def __init__(self, source: AgentProfileScopeSource) -> None:
        """绑定配置文件来源，建立空索引。

        参数:
            source: 作用域 → 配置目录 → profile 列表的来源。装配期显式注入；不接受缺省值，
                以免出现「注册表存在但配置永远不会被装载」的静默失效状态。

        返回:
            无。

        异常:
            无。

        副作用:
            只保存引用并构造两把锁与空索引，不读取文件系统。
        """

        self._source = source
        self._lock = threading.RLock()
        # 装载锁与索引锁分离：装载要做磁盘 IO，绝不能在持有索引锁时进行。
        self._load_lock = threading.Lock()
        self._profiles: dict[tuple[str, str], AgentProfile] = {}
        self._loaded_scopes: set[str] = set()

    @property
    def source(self) -> AgentProfileScopeSource:
        """返回本注册表使用的配置来源（只读）。

        供配置写入方复用同一目录映射：写目录与装载目录必须来自同一个来源，否则保存后的配置
        可能落在读取方看不到的位置，表现为「保存成功但列表为空」。
        """

        return self._source

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
        return normalize_scope_path(text)

    @staticmethod
    def _decide_registration(
        current: dict[tuple[str, str], AgentProfile],
        scope: str,
        profile: AgentProfile,
    ) -> tuple[bool, str | None]:
        """判定一份 profile 能否进入指定作用域（纯函数，不加锁、不写日志）。

        判定规则与历史实现一致：同一作用域已存在同 ``agent_id`` 视为重复；``agent_id`` 与
        另一作用域冲突时保留先注册者（system 基线不被 workspace 覆盖）。单条注册与整批装载
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
            无。重复与冲突都不抛错，以免单个冲突中断整批装载。

        副作用:
            持 :attr:`_lock` 原地写入当前进程内存索引；跳过时写 warning 日志（出锁后记录）——
            ``agent_profile_duplicate_skipped``（同作用域重复）或
            ``agent_profile_conflict_skipped``（跨作用域冲突），两者都带 ``scope`` 与
            ``agent_id``，冲突时额外带 ``conflict_scope``。注册 workspace 作用域前会先装载
            system 基线与该 workspace 作用域本身（首次注册该 workspace 时会各读一次配置目录），
            否则「先注册者优先」会因 system 尚未装载而让 workspace 的同名 profile 抢先进索引。
        """

        scope = self.normalize_workspace(workspace)
        if scope != self.SYSTEM_WORKSPACE:
            # （1）system 基线先入索引，跨作用域同名 id 由 system 胜出；
            # （2）目标作用域先装载，否则「先写入、后首次读取」时刚注册的 profile 会被目录内容
            #     重新读回并判为「同作用域重复」，写出与实际不符的 warning。
            self._ensure_scope(self.SYSTEM_WORKSPACE)
            self._ensure_scope(scope)
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

    def drop_scope(self, workspace: str | Path) -> None:
        """卸载指定作用域的索引与已装载标记。

        workspace 被删除时调用。懒装载「每个作用域只读盘一次」的前提是作用域身份稳定：
        workspace 删除后同一路径可能被重新创建，若保留标记就再也读不到新内容，已删除的
        profile 也会一直留在索引里。

        参数:
            workspace: 系统哨兵或 workspace 根路径。

        返回:
            无。

        异常:
            ValueError: workspace 路径为空时由 ``normalize_workspace`` 抛出。

        副作用:
            持「装载锁 → 索引锁」原地清空该作用域的 profile 并移除其已装载标记；不触碰
            文件系统与其他作用域。作用域不存在时幂等无操作。
        """

        scope = self.normalize_workspace(workspace)
        with self._load_lock:
            with self._lock:
                self._profiles = {
                    key: profile
                    for key, profile in self._profiles.items()
                    if key[0] != scope
                }
            self._loaded_scopes.discard(scope)

    def ensure_scope_loaded(self, workspace: str | Path) -> None:
        """确保指定作用域的配置已装载（幂等，已装载则直接返回）。

        供「先落盘、再注册」的写入方在写文件前调用：若该作用域尚未装载，写入方注册的 profile
        会在该作用域首次被读取时与刚写入的文件撞成「同作用域重复」，写出与实际不符的告警并让
        ``agent_profile_scope_loaded`` 的 ``skipped_count`` 变成噪声。

        参数:
            workspace: 系统哨兵或 workspace 根路径。

        返回:
            无。

        异常:
            ValueError: workspace 路径为空时由 ``normalize_workspace`` 抛出。

        副作用:
            该作用域尚未装载时读取其配置目录（非 system 作用域会连带装载 system 基线，
            见 :meth:`_ensure_scope`）；已装载时为纯内存操作。
        """

        self._ensure_scope(self.normalize_workspace(workspace))

    def _ensure_scope(self, scope: str) -> None:
        """确保指定作用域的配置已装载（每个作用域在本进程内只读盘一次）。

        参数:
            scope: 经 :meth:`normalize_workspace` 归一化后的作用域键。

        返回:
            无。

        异常:
            无：装载失败由 ``AgentProfileScopeSource`` 记日志并降级为空列表（坏配置不影响运行）。

        副作用:
            首次调用时读盘并原地写入内存索引；无论成功还是降级，都把该作用域标记为已装载，
            避免每次读取重复读盘与刷日志。出锁后写 ``agent_profile_scope_loaded`` info 日志
            （含作用域与装载数量），使「每个作用域只装载一次」可在运行期经日志验证；
            冲突与重复裁决同 :meth:`register`，跳过项另写 warning。

            **传入非 system 作用域时，会先递归确保 system 基线已装载**（因此一次 workspace
            读取可能额外触发一次系统配置目录读取与一条 ``agent_profile_scope_loaded`` 日志）。
            这是「system 基线不被 workspace 覆盖」在懒装载下的前置条件：跨作用域同名 id 按
            「先注册者优先」裁决，system 若晚于 workspace 入索引，其用户配置文件会被判为冲突
            而整项丢弃。该递归发生在装载锁之外（装载锁不可重入），锁序仍为「装载锁 → 索引锁」。
        """

        if scope != self.SYSTEM_WORKSPACE:
            self._ensure_scope(self.SYSTEM_WORKSPACE)
        with self._load_lock:
            if scope in self._loaded_scopes:
                return
            profiles = self._source.load(scope)
            duplicates, conflicts = self._merge_scope(scope, profiles)
            self._loaded_scopes.add(scope)
        log.info(
            "agent_profile_scope_loaded",
            extra={
                "msg": "Agent 配置作用域已按需装载",
                "data": {
                    "scope": scope,
                    "profile_count": len(profiles),
                    "skipped_count": len(duplicates) + len(conflicts),
                },
            },
        )
        for agent_id in duplicates:
            log.warning(
                "agent_profile_duplicate_skipped",
                extra={
                    "msg": "同一作用域内 Agent ID 重复，保留先读到的 profile",
                    "data": {"scope": scope, "agent_id": agent_id},
                },
            )
        for agent_id, conflict_scope in conflicts:
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

    def _merge_scope(
        self,
        scope: str,
        profiles: Iterable[AgentProfile],
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """把读到的 profile 合并进该作用域索引，已存在的同 id 一律保留先注册者。

        为什么是合并而不是整批替换：同一作用域里既有装配期由代码注册的内置 profile
        （system 作用域的 ``main_agent`` / ``general-assistant``），也有目录里的用户配置；
        整批替换会把内置 profile 一并清掉。合并配合 :meth:`_decide_registration` 的
        「先注册者优先」规则，正好保证内置 profile 不被用户文件覆盖。

        代价（已知限制）：装载只在该作用域首次被访问时发生，进程生命周期内不感知进程外的
        手工删改（配置中心自身的增删改仍走 :meth:`register` / :meth:`replace` /
        :meth:`unregister` 即时同步）。

        参数:
            scope: 已归一化的作用域键。
            profiles: 按文件名排序读到的 profile 序列。

        返回:
            ``(duplicates, conflicts)``：批内重复的 agent_id 列表，以及与其他作用域冲突的
            ``(agent_id, 先注册作用域)`` 列表，供调用方出锁后记录日志。

        异常:
            无。

        副作用:
            持 :attr:`_lock` 原地写入索引；调用方须已持有装载锁。
        """

        duplicates: list[str] = []
        conflicts: list[tuple[str, str]] = []
        with self._lock:
            for profile in profiles:
                accepted, conflict = self._decide_registration(self._profiles, scope, profile)
                if not accepted:
                    duplicates.append(profile.agent_id)
                    continue
                if conflict is not None:
                    conflicts.append((profile.agent_id, conflict))
                    continue
                self._profiles[(scope, profile.agent_id)] = profile
        return duplicates, conflicts

    def resolve(self, workspace: str | Path, agent_id: str) -> AgentProfile | None:
        """按 workspace 优先、system 回退的规则解析 profile。

        参数:
            workspace: 当前 workspace 根路径或系统哨兵。
            agent_id: 要解析的 Agent ID。

        返回:
            workspace 或 system 中匹配的 profile；未找到时返回 ``None``。

        异常:
            无。装载失败时该作用域按「无配置」参与解析，只可能回退到 system 或返回 ``None``。

        副作用:
            若该作用域尚未装载，则读取它的配置文件（非 system 作用域会连带装载 system 基线，
            见 :meth:`_ensure_scope`）；此后仅读取内存索引。
        """

        scope = self.normalize_workspace(workspace)
        # ``_ensure_scope`` 已保证非 system 作用域先装载 system 基线，无需在此重复调用。
        self._ensure_scope(scope)
        # 两次查询在同一临界区内完成，不会读到跨写入的混合视图（workspace 未命中才回退 system）。
        with self._lock:
            return self._profiles.get((scope, agent_id)) or self._profiles.get(
                (self.SYSTEM_WORKSPACE, agent_id)
            )

    def resolve_local(self, workspace: str | Path, agent_id: str) -> AgentProfile | None:
        """只解析指定作用域内的 Agent，不执行 system fallback。

        配置写入、更新和删除必须使用本方法区分 workspace 本地文件与 system 继承项，
        不得用带 fallback 的 :meth:`resolve` 代替。

        参数:
            workspace: 系统哨兵或 workspace 根路径。
            agent_id: 要查找的 Agent 标识。

        返回:
            指定作用域内的 profile；不存在时返回 ``None``。

        异常:
            ValueError: workspace 路径为空时由 ``normalize_workspace`` 抛出。

        副作用:
            若该作用域尚未装载，则读取它的配置文件（非 system 作用域会连带装载 system 基线，见
            :meth:`_ensure_scope`）；此后只读取受锁保护的进程内索引。
        """

        scope = self.normalize_workspace(workspace)
        self._ensure_scope(scope)
        with self._lock:
            return self._profiles.get((scope, agent_id))

    def list(self, workspace: str | Path) -> list[AgentProfile]:
        """列出指定 workspace 可见的 profile；系统作用域 profile 排在 workspace profile 前。

        参数:
            workspace: 当前 workspace 根路径或系统哨兵。

        返回:
            system 与当前 workspace 作用域内的 profile 列表，不包含其他 workspace。

        异常:
            无。装载失败时该作用域按「无配置」参与列举。

        副作用:
            若该作用域尚未装载，则读取它的配置文件（非 system 作用域会连带装载 system 基线，
            见 :meth:`_ensure_scope`）；此后仅读取内存索引。
        """

        scope = self.normalize_workspace(workspace)
        # ``_ensure_scope`` 已保证非 system 作用域先装载 system 基线，无需在此重复调用。
        self._ensure_scope(scope)
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
            无。

        副作用:
            若该作用域尚未装载，则读取它的配置文件（非 system 作用域会连带装载 system 基线，见
            :meth:`_ensure_scope`）；此后仅读取内存索引。
        """

        return {profile.agent_id for profile in self.list(workspace)}

    def child_agent_summary(self, workspace: str | Path) -> str:
        """将指定 workspace 可见的 CHILD profile 投影为委派能力摘要。

        参数:
            workspace: 当前 workspace 根路径或系统哨兵。

        返回:
            可供父 Agent 读取的子 Agent ID 与 description 摘要；没有 CHILD 时为空字符串。

        异常:
            无。

        副作用:
            若该作用域尚未装载，则读取它的配置文件（非 system 作用域会连带装载 system 基线，见
            :meth:`_ensure_scope`）；此后仅读取内存索引，不包含系统提示词正文。
        """

        blocks = [
            f"agent_id: {profile.agent_id} | description: {profile.description or ''} | allowed_tools: {profile.allowed_tools}"
            for profile in self.list(workspace)
            if profile.agent_type is AgentProfileType.CHILD
        ]
        if not blocks:
            return "no child agents available"
        return "Available child agents:\n" + "\n".join(blocks)

    def child_agent_ids(self, workspace: str | Path) -> set[str]:
        """列出指定 workspace 可见的 CHILD Agent ID。

        参数:
            workspace: 当前 workspace 根路径或系统哨兵。

        返回:
            system 与当前 workspace 可见的 CHILD ID 集合。

        异常:
            无。

        副作用:
            若该作用域尚未装载，则读取它的配置文件（非 system 作用域会连带装载 system 基线，见
            :meth:`_ensure_scope`）；此后仅读取内存索引。
        """

        return {
            profile.agent_id
            for profile in self.list(workspace)
            if profile.agent_type is AgentProfileType.CHILD
        }

    def list_system_sub_agents(self) -> list[AgentProfile]:
        """列出系统作用域内已注册的 CHILD profile，不包含主 Agent。

        返回:
            系统作用域中 ``agent_type`` 为 CHILD 的 profile 列表。

        异常:
            无。

        副作用:
            首次调用时读取一次系统配置目录；此后仅读取内存索引。
        """

        self._ensure_scope(self.SYSTEM_WORKSPACE)
        with self._lock:
            return [
                profile
                for (profile_scope, _), profile in self._profiles.items()
                if profile_scope == self.SYSTEM_WORKSPACE
                and profile.agent_type is AgentProfileType.CHILD
            ]

    def list_workspace_sub_agents(self, workspace: str | Path) -> list[AgentProfile]:
        """列出指定 workspace 作用域内的 Agent profile。

        与 :meth:`list` 的区别是**不合并** system 作用域，供配置中心区分本地文件与继承项。

        参数：
            workspace: 当前 workspace 根路径或系统哨兵。

        返回：
            当前作用域内的 profile 列表；调用方获得的是 Registry 中的共享不可变值对象。

        异常：
            ValueError: workspace 路径为空时由 ``normalize_workspace`` 抛出。

        副作用：
            若该作用域尚未装载，则读取它的配置文件（非 system 作用域会连带装载 system 基线，见
            :meth:`_ensure_scope`）；此后仅读取受锁保护的进程内索引。
        """

        scope = self.normalize_workspace(workspace)
        self._ensure_scope(scope)
        with self._lock:
            return [
                profile
                for (profile_scope, _), profile in self._profiles.items()
                if profile_scope == scope
            ]
