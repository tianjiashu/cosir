import type { Outline, OutlineEntry } from "./outline";

/**
 * 快照大纲 → 给模型读的文本：每种视图只决定「出哪些行」，行的写法都一样（缩进表示层级，`- ` 开头）。纯函数。
 *
 * 快照自己守字符预算：超过就截断并告诉模型从第几行接着读（`browser_snapshot` 的 `from_line`），不依赖后端的输出
 * 预算去截（那会切在任意位置）。分页每次重新读树，宿主不缓存上一次的行：ref 稳定，所以页面在两次读取之间变了也
 * 不会点错，代价只是行号可能有少量重叠或跳过。
 */

const SNAPSHOT_CHAR_BUDGET = 12_000;

/** 选哪部分：整页、只看视口、或按关键词查找（`find`，不区分大小写的子串，与 `scope` 可同时用）。 */
export type SnapshotView = { scope?: "page" | "viewport"; find?: string; fromLine?: number; budget?: number };

/**
 * 按视图出文本，从 `fromLine` 起按整行装进预算（行号是这个视图里的行号）。筛选视图只留选中的行与它们的祖先：
 * 没有祖先，模型分不清「删除」按钮是哪一条记录里的。筛选时先说明看的是哪部分、占整页多少行，模型据此知道还有
 * 没看到的内容、要不要换整页读。
 */
export function snapshotView(outline: Outline, { scope = "page", find, fromLine = 1, budget = SNAPSHOT_CHAR_BUDGET }: SnapshotView = {}): string {
  if (outline.entries.length === 0) return "(the page has no readable content)";
  const query = find?.toLowerCase();
  const filtered = scope === "viewport" || query !== undefined;
  if (!filtered) return paged(outline.entries.map(line), fromLine, budget);
  const chosen = withAncestors(outline.entries, (entry) => (scope !== "viewport" || entry.visible) && (query === undefined || entry.text.toLowerCase().includes(query)));
  const what = [scope === "viewport" ? "the part of the page in the viewport" : "", query === undefined ? "" : `lines matching ${JSON.stringify(find)}`].filter(Boolean).join(", ");
  if (chosen.length === 0) return `(nothing on the page: ${what})`;
  const lines = chosen.map(line);
  return `(showing ${what}, with their ancestors: ${lines.length} of ${outline.entries.length} lines)\n${paged(lines, fromLine, budget)}`;
}

/**
 * 变化超过当前页面行数的这个比例就给整页：变化多到增量和整页差不多长时，整页更好读（单页应用切换路由也落在这里）。
 * 验收时按真实页面调。
 */
const CHANGES_MAX_SHARE = 0.4;

/**
 * act 之后的增量：与这个标签上一次交给 Agent 的大纲（`previous`）相比，新出现、文字变了或换了位置（父条目变了）的
 * 行，连同它们的祖先；消失的行只列最外层那个。按条目的稳定键比对：ref 也按节点稳定，没出现在增量里的元素沿用
 * 之前的 ref。变化太多或超出预算时返回 null，由调用方给整页。
 *
 * 基准是宿主记的「上次交出去的」，不保证还在模型的上下文里（编辑重跑撤掉了那一轮、调用中途被取消），所以开头
 * 说清比的是什么、怎么拿整页。
 */
export function changesView(previous: Outline, current: Outline, { budget = SNAPSHOT_CHAR_BUDGET }: { budget?: number } = {}): string | null {
  const before = new Map(previous.entries.map((entry) => [entry.key, entry]));
  const now = new Set(current.entries.map((entry) => entry.key));
  const parentKey = (entries: readonly OutlineEntry[], entry: OutlineEntry) => entries[entry.parent]?.key;
  const marks = new Map<number, "new" | "changed">();
  current.entries.forEach((entry, index) => {
    const old = before.get(entry.key);
    if (!old) marks.set(index, "new");
    else if (old.text !== entry.text || parentKey(previous.entries, old) !== parentKey(current.entries, entry)) marks.set(index, "changed");
  });
  const removed = previous.entries.filter((entry) => !now.has(entry.key) && (entry.parent < 0 || now.has(previous.entries[entry.parent].key)));
  if (marks.size === 0 && removed.length === 0) return "(nothing on the page changed since this tab's last browser result)";
  if (marks.size + removed.length > current.entries.length * CHANGES_MAX_SHARE) return null;
  const shown = withAncestorIndexes(current.entries, (_, index) => marks.has(index));
  const lines = [
    `(changes since this tab's last browser result: ${marks.size} new or changed, ${removed.length} removed; unchanged lines are left out and their refs still work. Call browser_snapshot for the whole page.)`,
    ...shown.map((index) => line(current.entries[index]) + (marks.has(index) ? ` [${marks.get(index)}]` : "")),
    ...(removed.length ? ["removed:", ...removed.map((entry) => `- ${entry.text}`)] : []),
  ];
  const text = lines.join("\n");
  return text.length > budget ? null : text;
}

/** 选中的条目加上它们的全部祖先，保持原顺序。 */
function withAncestors(entries: readonly OutlineEntry[], keep: (entry: OutlineEntry) => boolean): OutlineEntry[] {
  return withAncestorIndexes(entries, keep).map((index) => entries[index]);
}

function withAncestorIndexes(entries: readonly OutlineEntry[], keep: (entry: OutlineEntry, index: number) => boolean): number[] {
  const included = new Set<number>();
  entries.forEach((entry, index) => {
    if (!keep(entry, index)) return;
    for (let at = index; at >= 0 && !included.has(at); at = entries[at].parent) included.add(at);
  });
  return [...included].sort((a, b) => a - b);
}

function line(entry: OutlineEntry): string {
  return `${"  ".repeat(entry.depth)}- ${entry.text}`;
}

/** 从 `fromLine` 起按整行装进预算；前后有没输出的行都说明，并给出接着读的行号。 */
function paged(lines: string[], fromLine: number, budget: number): string {
  if (fromLine > lines.length) return `(the snapshot has only ${lines.length} lines)`;
  const start = fromLine - 1;
  const head = start > 0 ? [`… lines 1-${start} not shown (from_line=${fromLine})`] : [];
  let used = 0;
  for (let index = start; index < lines.length; index += 1) {
    used += lines[index].length + 1;
    if (used > budget && index > start) {
      const rest = lines.length - index;
      return [...head, ...lines.slice(start, index), `… ${rest} more lines not shown; read on with from_line=${index + 1}`].join("\n");
    }
  }
  return [...head, ...lines.slice(start)].join("\n");
}
