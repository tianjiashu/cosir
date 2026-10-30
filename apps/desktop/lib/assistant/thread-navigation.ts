/**
 * 对话之外的入口（如后台任务看板）定位到对话里的某张工具卡片。
 *
 * 对话是虚拟列表、且只加载了最近一段历史：目标卡片可能没挂载，也可能还没加载，不能靠 DOM 查找。
 * 每个挂着对话列表的 Task 在这里登记一个导航器，由列表负责「按需往上翻页直到找到 → 滚到那一行」。
 * 同一 Task 只认最后登记的那个（对话页与工作台阅读器不会同时挂同一个 Task）。
 */
export type ThreadNavigator = {
  /** 滚到含该工具调用的消息；历史里找不到时返回 false。 */
  revealToolCall: (toolCallId: string) => Promise<boolean>;
};

const navigators = new Map<number, ThreadNavigator>();

export function registerThreadNavigator(taskId: number, navigator: ThreadNavigator): () => void {
  navigators.set(taskId, navigator);
  return () => {
    if (navigators.get(taskId) === navigator) navigators.delete(taskId);
  };
}

/** 该 Task 的对话没挂着（例如不在当前页面）或卡片不在历史里时返回 false。 */
export async function revealToolCall(taskId: number, toolCallId: string): Promise<boolean> {
  return (await navigators.get(taskId)?.revealToolCall(toolCallId)) ?? false;
}
