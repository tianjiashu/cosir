import type {
  ObservedMessage,
  ObservedRun,
  ObservedStatusChange,
} from "@/lib/api/session-observation";

/** 运行节点：运行的一次状态迁移，插在消息行之间解释「这里为什么停、停了多久」。 */
export type RunNode = { key: string; change: ObservedStatusChange; run: ObservedRun };

export type TimelineItem =
  | { type: "message"; message: ObservedMessage }
  | { type: "node"; node: RunNode };

export function runNodes(runs: ObservedRun[]): RunNode[] {
  return runs
    .flatMap((run, runIndex) =>
      run.changes.map((change, index) => ({ key: `node-${runIndex}-${index}-${change.at}`, change, run })),
    )
    .sort((left, right) => Date.parse(left.change.at) - Date.parse(right.change.at));
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function nodeTitle(change: ObservedStatusChange): string {
  if (change.previous_status === null) return "运行开始";
  if (change.status === "running") return change.previous_status === "suspended" ? "被唤醒" : "恢复运行";
  return (
    (
      {
        suspended: "挂起",
        completed: "运行完成",
        cancelled: "已停止",
        failed: "运行失败",
      } as Record<string, string>
    )[change.status] ?? change.status
  );
}

export function nodeTone(change: ObservedStatusChange): "neutral" | "warning" | "destructive" {
  return change.status === "failed" ? "destructive" : change.status === "cancelled" ? "warning" : "neutral";
}

export function formatDuration(milliseconds: number): string {
  const seconds = Math.round(milliseconds / 1000);
  if (seconds < 1) return "不到 1 秒";
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

const END_REASONS: Record<string, string> = {
  user_cancelled: "用户停止",
  parent_cancelled: "上游收回",
};

/** 节点的补充说明：离上一个节点多久、结束原因，以及挂在运行最后一次迁移上的结局与用量。 */
export function nodeNotes({ change, run }: RunNode): string[] {
  const notes: string[] = [];
  if (change.previous_at) {
    const elapsed = formatDuration(Date.parse(change.at) - Date.parse(change.previous_at));
    notes.push(
      change.status !== "running"
        ? `运行了 ${elapsed}`
        : change.previous_status === "suspended"
          ? `挂起了 ${elapsed}`
          : `停了 ${elapsed}`,
    );
  }
  if (change.end_reason) notes.push(END_REASONS[change.end_reason] ?? change.end_reason);
  if (change.latest && TERMINAL.has(change.status)) {
    if (run.error_code) notes.push(run.error_code);
    const total = run.usage?.total_tokens;
    if (typeof total === "number") notes.push(`整个运行 ${new Intl.NumberFormat("zh-CN").format(total)} tokens`);
  }
  return notes;
}

/**
 * 把节点按时刻插进消息行之间。`messages` 是列表当前的显示顺序（按序号或创建时间），`ascending`
 * 是它的方向；节点排在第一条比它晚创建的消息之前，其余落在末尾。
 */
export function mergeTimeline(messages: ObservedMessage[], nodes: RunNode[], ascending: boolean): TimelineItem[] {
  const ordered = ascending ? messages : [...messages].reverse();
  const items: TimelineItem[] = [];
  let next = 0;
  for (const message of ordered) {
    const created = Date.parse(message.created_at);
    while (next < nodes.length && Date.parse(nodes[next].change.at) < created)
      items.push({ type: "node", node: nodes[next++] });
    items.push({ type: "message", message });
  }
  while (next < nodes.length) items.push({ type: "node", node: nodes[next++] });
  return ascending ? items : items.reverse();
}
