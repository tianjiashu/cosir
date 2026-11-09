/**
 * 勾选列表的一次点击：普通点击切换这一项；Shift 点击把「锚点到这一项」整段设成这一项的新状态
 * （与文件管理器的连选一致，范围按当前可见顺序算，搜索过滤掉的项不受影响）。
 *
 * 锚点不在当前可见列表里（被搜索过滤掉或还没点过）时退化为普通切换。返回新集合，不改入参。
 */
export function toggleSelection<T>(
  visible: readonly T[],
  selected: ReadonlySet<T>,
  target: T,
  { anchor, range }: { anchor: T | null; range: boolean },
): Set<T> {
  const next = new Set(selected);
  const checked = !selected.has(target);
  const from = range && anchor !== null ? visible.indexOf(anchor) : -1;
  const to = visible.indexOf(target);
  const span = from >= 0 && to >= 0
    ? visible.slice(Math.min(from, to), Math.max(from, to) + 1)
    : [target];
  for (const item of span) {
    if (checked) next.add(item);
    else next.delete(item);
  }
  return next;
}
