import { HostMethodError } from "../host-link/methods";

/** 两个标签上限；事实源是后端配置中心，每次请求随参数带来，宿主不另存配置。 */
export type TabLimits = { perTask: number; total: number };

/**
 * 标签页的归属与上限：`tabId → taskId`，以及每个 Task 的当前标签。不碰 Electron，只管簿记。
 *
 * 归属是硬边界：不属于请求方 Task 的标签一律报 `TAB_NOT_FOUND`，不区分「不存在」与「属于别人」，
 * 父子 Agent 各用各的标签页、互不可见。`tabId` 在宿主进程内单调递增（`t1`、`t2`…），关闭后不复用，
 * 模型手里的旧 id 不会意外指到新标签。
 */
export class TabTable {
  private sequence = 0;
  /** 按创建顺序保存，`tabsOf` 与当前标签的回落都依赖这个顺序。 */
  private readonly owners = new Map<string, number>();
  private readonly current = new Map<number, string>();

  /** 超出上限时抛 `TAB_LIMIT`，消息说明是本 Task 满了还是全局满了。 */
  assertCanOpen(taskId: number, limits: TabLimits): void {
    if (this.tabsOf(taskId).length >= limits.perTask) {
      throw new HostMethodError("TAB_LIMIT", `this task already has ${limits.perTask} tabs open (per-task limit)`);
    }
    if (this.owners.size >= limits.total) {
      throw new HostMethodError("TAB_LIMIT", `the browser already has ${limits.total} tabs open across all tasks (global limit)`);
    }
  }

  /** 登记新标签并设为该 Task 的当前标签；上限由调用方先经 `assertCanOpen` 检查。 */
  add(taskId: number): string {
    this.sequence += 1;
    const tabId = `t${this.sequence}`;
    this.owners.set(tabId, taskId);
    this.current.set(taskId, tabId);
    return tabId;
  }

  /** 解析请求要操作的标签：显式给出的须属于该 Task，没给出时取当前标签。 */
  require(taskId: number, tabId?: string): string {
    const resolved = tabId ?? this.current.get(taskId);
    if (resolved === undefined || this.owners.get(resolved) !== taskId) {
      throw new HostMethodError("TAB_NOT_FOUND", tabId ? `tab ${tabId} is not open in this task` : "this task has no open tab");
    }
    return resolved;
  }

  /** 该 Task 有没有当前标签（没有时导航会新开一个）。 */
  hasCurrent(taskId: number): boolean {
    return this.current.has(taskId);
  }

  select(taskId: number, tabId: string): void {
    this.current.set(taskId, this.require(taskId, tabId));
  }

  /** 移除标签；它若是当前标签，当前标签回落到该 Task 最近创建的另一个标签。 */
  remove(tabId: string): void {
    const taskId = this.owners.get(tabId);
    if (taskId === undefined) return;
    this.owners.delete(tabId);
    if (this.current.get(taskId) !== tabId) return;
    const remaining = this.tabsOf(taskId);
    if (remaining.length > 0) this.current.set(taskId, remaining[remaining.length - 1]);
    else this.current.delete(taskId);
  }

  tabsOf(taskId: number): string[] {
    return [...this.owners].filter(([, owner]) => owner === taskId).map(([tabId]) => tabId);
  }

  ownerOf(tabId: string): number | undefined {
    return this.owners.get(tabId);
  }

  allTabs(): string[] {
    return [...this.owners.keys()];
  }
}
