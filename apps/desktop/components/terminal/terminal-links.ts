import { WebLinksAddon } from "@xterm/addon-web-links";
import type { IBuffer, IBufferCell, IDisposable, ILink, Terminal } from "@xterm/xterm";

import { resolveLink, resolvePathText, workspaceRootPattern, type LinkTarget } from "@/lib/links/resolve-link";

export type TerminalLinkOptions = {
  /** 每次识别时现读：视图只按身份创建一次，根目录到达或变化不重建 xterm。 */
  getWorkspaceRoot: () => string | null | undefined;
  /** 打开入口（`useOpenLink`）：网址与工作区路径都经它，确认与分派不在终端里做。 */
  onOpenLink: (target: LinkTarget) => void;
};

/**
 * 给一个 xterm 实例接上链接识别：网页地址（`WebLinksAddon`）与工作区内的绝对路径。底部终端与对话里的
 * 命令卡片共用这一处，两边点到的东西一致。
 *
 * 链接按「识别 → 解析 → 打开」分层：这里只找候选，「根目录在文本里长什么样」与「是否属于工作区」都由
 * `lib/links/resolve-link.ts` 决定（`workspaceRootPattern` / `resolveLink`，同一比较口径），识别层不另写
 * 一份大小写与分隔符规则。相对路径不识别：它相对的是 shell 的当前目录，终端不知道，按工作区根解析会打开
 * 错的文件。
 *
 * 返回的 disposable 随视图释放。
 */
export function registerTerminalLinks(terminal: Terminal, options: TerminalLinkOptions): IDisposable[] {
  const web = new WebLinksAddon((event, uri) => {
    event.preventDefault();
    const target = resolveLink(uri, { workspaceRoot: null });
    if (target) options.onOpenLink(target);
  });
  terminal.loadAddon(web);
  const paths = terminal.registerLinkProvider({
    provideLinks: (lineNumber, callback) => {
      const root = options.getWorkspaceRoot();
      const links = root ? workspacePathLinks(terminal.buffer.active, lineNumber, root, options.onOpenLink) : [];
      callback(links.length > 0 ? links : undefined);
    },
  });
  return [web, paths];
}

/** 根目录前不能紧挨着路径字符：`/other/repo/…` 里的 `/repo` 不算。 */
const BEFORE_ROOT = String.raw`(?<![\w.~/\\-])`;
/** 根目录后必须是分隔符或路径的结束：根 `/repo` 不认 `/repository`。 */
const AFTER_ROOT = String.raw`(?=$|[\\/\s'"\`;,<>{}|*?:()[\]])`;
/**
 * 根目录之后的部分，到空白、引号或包围标点为止。`( ) [ ]` 允许出现（Next.js 的 `[slug]`、`(group)` 目录），
 * 末尾不成对的由 `trimTrailing` 剥掉；`:` 不在路径里，留给行列后缀。
 */
const PATH_TAIL = String.raw`(?:[\\/][^\s'"\`;,<>{}|*?:]*)?`;
const LINE_SUFFIX = String.raw`(?::\d+(?::\d+)?)?`;

/** 识别时以当前行为中心最多拼这么多字符（与 `WebLinksAddon` 的窗口相同）：一行压缩 JSON 能折成上千行。 */
const MAX_WINDOW_CHARS = 2048;

/**
 * 工作区路径候选：先找工作区根目录，再向后延伸，可带 `:行[:列]`。从根目录锚定而不是按通用的绝对路径形态找：
 * 根目录本身可能含空格（Windows 用户名常见），通用形态会在空格处截断。导出供单测。
 */
export function findWorkspacePathCandidates(text: string, workspaceRoot: string): { start: number; text: string }[] {
  const pattern = new RegExp(`${BEFORE_ROOT}${workspaceRootPattern(workspaceRoot)}${AFTER_ROOT}${PATH_TAIL}${LINE_SUFFIX}`, "gi");
  const candidates: { start: number; text: string }[] = [];
  for (const match of text.matchAll(pattern)) {
    candidates.push({ start: match.index ?? 0, text: trimTrailing(match[0]) });
  }
  return candidates;
}

/** 剥掉句末的点与不成对的右括号：`(see /repo/a.ts).` 只取 `/repo/a.ts`。 */
function trimTrailing(value: string): string {
  let result = value;
  for (;;) {
    const last = result.at(-1);
    const unbalanced = (open: string, close: string) => last === close && result.split(close).length > result.split(open).length;
    if (last === "." || unbalanced("(", ")") || unbalanced("[", "]")) result = result.slice(0, -1);
    else return result;
  }
}

/**
 * 第 `lineNumber` 行（1 起算）所在逻辑行里属于工作区的路径链接。
 *
 * 逻辑行把软折行（`isWrapped`）拼回一整行再匹配，长路径折行后照样可点；下标按单元格换算，行内有中文、
 * emoji 等宽字符时范围不偏移。`WebLinkProvider` 里同类的换算是私有实现，这里用公开的单元格 API 另写。
 */
export function workspacePathLinks(
  buffer: IBuffer,
  lineNumber: number,
  workspaceRoot: string,
  open: (target: LinkTarget) => void,
): ILink[] {
  const { text, cells } = logicalLine(buffer, lineNumber - 1);
  const links: ILink[] = [];
  for (const candidate of findWorkspacePathCandidates(text, workspaceRoot)) {
    const target = resolvePathText(candidate.text, { workspaceRoot });
    if (target?.kind !== "workspace") continue;
    const first = cells[candidate.start];
    const last = cells[candidate.start + candidate.text.length - 1];
    if (first === undefined || last === undefined) continue;
    const start = { x: first.x + 1, y: first.y + 1 };
    const end = { x: last.x + last.width, y: last.y + 1 };
    if (end.y < lineNumber || start.y > lineNumber) continue;
    links.push({ text: candidate.text, range: { start, end }, activate: () => open(target) });
  }
  return links;
}

type CellPosition = { x: number; y: number; width: number };

/**
 * 把 `row` 所在的软折行段拼成一行文本，并给出每个字符（UTF-16 下标）落在哪个单元格；只拼以 `row` 为中心
 * 约 `MAX_WINDOW_CHARS` 个字符的窗口。
 */
function logicalLine(buffer: IBuffer, row: number): { text: string; cells: CellPosition[] } {
  const reach = Math.ceil(MAX_WINDOW_CHARS / Math.max(1, buffer.getLine(row)?.length ?? 1));
  let first = row;
  while (first > row - reach && first > 0 && buffer.getLine(first)?.isWrapped) first--;
  let last = row;
  while (last < row + reach && buffer.getLine(last + 1)?.isWrapped) last++;
  let text = "";
  const cells: CellPosition[] = [];
  let cell: IBufferCell | undefined;
  for (let y = first; y <= last; y++) {
    const line = buffer.getLine(y);
    if (!line) break;
    for (let x = 0; x < line.length; x++) {
      cell = line.getCell(x, cell);
      if (!cell) break;
      const width = cell.getWidth();
      // 宽字符占两格，第二格是宽度为 0 的占位，不对应任何字符。
      if (width === 0) continue;
      const chars = cell.getChars() || " ";
      for (let i = 0; i < chars.length; i++) cells.push({ x, y, width });
      text += chars;
    }
  }
  return { text, cells };
}
