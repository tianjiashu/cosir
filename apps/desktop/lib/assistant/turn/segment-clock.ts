/**
 * 活动段的计时起点：前端本地记下每段第一次被看到在运行的时刻。
 *
 * 后端没有活动段的时间事实（段是两次中断之间的展示单位），这里只服务运行中的连续计时：思考与工具调用共用
 * 一个起点，出现新旁白另起一段、重新计时。按「会话 · 回复 · 段 key」记，虚拟列表卸载再挂载、组件重渲染都
 * 沿用同一个起点。段内已有更早开跑的调用时把起点前移到它并记住——运行中刷新页面至少能从那次调用算起；记住
 * 而不是每次取较小值，是因为调用结束时后端会清掉它的 `startedAt`，否则计时会往回跳。
 * 不持久化、不随会话删除回收：每段一个时间戳，进程内的量可以忽略。
 */
const starts = new Map<string, number>();

/** 返回这一段的计时起点（ISO）；`earliest` 是段内已开跑调用的最早 `startedAt`。 */
export function segmentStartedAt(sessionId: number, messageId: string, segmentKey: string, earliest: string | undefined): string {
  const key = `${sessionId}\u0000${messageId}\u0000${segmentKey}`;
  const anchor = earliest === undefined ? Number.NaN : Date.parse(earliest);
  const start = Math.min(starts.get(key) ?? Date.now(), Number.isNaN(anchor) ? Infinity : anchor);
  starts.set(key, start);
  return new Date(start).toISOString();
}
