import type { RefRegistry } from "../ref-registry";
import { isFrameNode, isFrameRole, stringValue, type AXNode, type SnapshotDocument } from "./document";

/**
 * 读到的文档树 → 快照大纲：给模型读的一行行条目，各种视图（`views.ts`）都从它渲染。纯函数，不碰 Electron。
 *
 * 行的形状对齐业界常见的 aria 快照（`role "name" [ref=eN] [状态]`，按层级缩进，与 Playwright 的 AI 快照同一写法），
 * 模型据 ref 指定要操作的元素。取舍：
 * - 只给能操作的元素分配 ref：可交互角色，加上「看起来可点」的无语义元素（`<div onclick>` 之类：无障碍树里是
 *   无名 `generic`，只有光标样式说明它能点，标 `[cursor=pointer]`）。cursor 会继承给子元素，只认最外层那个
 *   （`pointerRoots`），否则一张卡片里每一层都会拿到 ref。其余节点只作为上下文文字出现。
 * - 链接带上地址（无障碍树的 `url` 属性已是绝对地址）：同源只写路径，省预算；模型可以直接导航，不必先点。
 * - ref 由调用方传入的 `RefRegistry` 分配（按节点稳定，见 `ref-registry.ts`），在这里对整棵树分配：任何视图（分页、
 *   视口内、查找、增量）里出现的 ref 都是同一套，不随视图而变。
 * - 无名的容器角色折叠掉、相邻文字合并成一行：真实页面的树里绝大多数是这类噪声。
 * - 子框架（iframe）的内容不在上一层的树里：读到了的（`read.ts`，有上限）接在 iframe 那一行下面；没读到的只标出
 *   位置，并计入数量。
 * - 每行带稳定的键（会话 + 无障碍节点 id）：增量视图按它比对前后两份大纲。合并的文字行按「所在节点 + 第几段」：
 *   文字节点常被整个替换（`textContent` 赋值、框架重新渲染），按节点认会把「改了字」报成一增一删。
 */

export type OutlineEntry = {
  /** 稳定键：同一个节点在同一文档里每次都相同（无障碍节点 id 即 DOM 节点 id，实测）；文字行见模块说明。 */
  key: string;
  depth: number;
  /** 一行的内容，不含缩进与列表符。 */
  text: string;
  /** 上一层条目的下标，顶层为 -1：筛选视图据此带上祖先。 */
  parent: number;
  /** 与视口里看得见的区域相交（合并的文字行看其中任一段；没有布局的节点为 false，靠子孙带出来）。 */
  visible: boolean;
};

export type Outline = {
  entries: OutlineEntry[];
  /** 页面里没有展开的子框架数量。 */
  frames: number;
};

const NAME_MAX_CHARS = 200;
const URL_MAX_CHARS = 120;

const INTERACTIVE_ROLES = new Set([
  "button", "checkbox", "combobox", "link", "listbox", "menuitem", "menuitemcheckbox", "menuitemradio",
  "option", "radio", "searchbox", "slider", "spinbutton", "switch", "tab", "textbox", "treeitem",
]);
/** 没有名字时不单独成行、直接展开子节点的容器角色。 */
const TRANSPARENT_ROLES = new Set(["generic", "none", "GenericContainer", "group", "LayoutTable", "LayoutTableRow", "LayoutTableCell", "Section", "paragraph", "div"]);
const SKIPPED_ROLES = new Set(["InlineTextBox", "LineBreak"]);
const TEXT_ROLES = new Set(["StaticText", "text"]);
const REPORTED_STATES = ["checked", "selected", "expanded", "disabled", "required", "pressed"] as const;

export function buildOutline(document: SnapshotDocument, refs: RefRegistry, { pageUrl }: { pageUrl?: string } = {}): Outline {
  const entries: OutlineEntry[] = [];
  let frames = 0;

  const render = (doc: SnapshotDocument, depth: number, parent: number): void => {
    const byId = new Map(doc.nodes.map((node) => [node.nodeId, node]));
    const root = doc.nodes.find((node) => !node.parentId || !byId.has(node.parentId));
    const keyOf = (node: AXNode) => `${doc.sessionId ?? ""}:${node.nodeId}`;
    const inView = (node: AXNode) => node.backendDOMNodeId !== undefined && doc.inView.has(node.backendDOMNodeId);
    const push = (key: string, text: string, depth: number, parent: number, visible: boolean): number => {
      entries.push({ key, depth, text, parent, visible });
      return entries.length - 1;
    };

    const visit = (node: AXNode, depth: number, parent: number): void => {
      const role = stringValue(node.role);
      if (SKIPPED_ROLES.has(role)) return;
      const name = clip(stringValue(node.name));
      if (isFrameRole(role)) {
        if (!isFrameNode(node)) return;
        const content = node.backendDOMNodeId === undefined ? undefined : doc.frames.get(node.backendDOMNodeId);
        const label = `iframe${name ? ` "${name}"` : ""}`;
        if (content) {
          render(content, depth + 1, push(keyOf(node), label, depth, parent, inView(node)));
        } else {
          frames += 1;
          push(keyOf(node), `${label} (contents not shown)`, depth, parent, inView(node));
        }
        return;
      }
      const interactive = INTERACTIVE_ROLES.has(role);
      const clickable = !interactive && !node.ignored && node.backendDOMNodeId !== undefined && doc.pointer.has(node.backendDOMNodeId);
      const transparent = !clickable && (node.ignored || role === "RootWebArea" || (!name && TRANSPARENT_ROLES.has(role)));
      if (transparent) {
        visitChildren(node, depth, parent);
        return;
      }
      let line = `${role}${name ? ` "${name}"` : ""}`;
      if ((interactive || clickable) && node.backendDOMNodeId !== undefined) {
        line += ` [ref=${refs.assign({ sessionId: doc.sessionId, backendNodeId: node.backendDOMNodeId })}]`;
      }
      if (clickable) line += " [cursor=pointer]";
      line += states(node);
      const url = role === "link" ? linkUrl(node, pageUrl) : "";
      if (url) line += ` url="${url}"`;
      const value = clip(stringValue(node.value));
      if (value && value !== name) line += ` value="${value}"`;
      visitChildren(node, depth + 1, push(keyOf(node), line, depth, parent, inView(node)));
    };

    const visitChildren = (node: AXNode, depth: number, parent: number): void => {
      let text: AXNode[] = [];
      let runs = 0;
      const flush = () => {
        const joined = clip(text.map((item) => stringValue(item.name)).join(" ").replace(/\s+/g, " ").trim());
        if (joined) push(`${keyOf(node)}/text${runs++}`, `text "${joined}"`, depth, parent, text.some(inView));
        text = [];
      };
      for (const childId of node.childIds ?? []) {
        const child = byId.get(childId);
        if (!child) continue;
        if (!child.ignored && TEXT_ROLES.has(stringValue(child.role))) {
          text.push(child);
          continue;
        }
        flush();
        visit(child, depth, parent);
      }
      flush();
    };

    if (root) visit(root, depth, parent);
  };

  render(document, 0, -1);
  return { entries, frames };
}

function states(node: AXNode): string {
  let result = "";
  for (const property of node.properties ?? []) {
    const value = property.value.value;
    if (property.name === "level" && typeof value === "number") result += ` [level=${value}]`;
    else if ((REPORTED_STATES as readonly string[]).includes(property.name)) {
      if (value === true || value === "true") result += ` [${property.name}]`;
      else if (value === "mixed") result += ` [${property.name}=mixed]`;
    }
  }
  return result;
}

/** 链接地址：同源只写路径（含查询与锚点），其余写全；过长截断。 */
function linkUrl(node: AXNode, pageUrl: string | undefined): string {
  const raw = stringValue(node.properties?.find((property) => property.name === "url")?.value);
  if (!raw) return "";
  let shown = raw;
  if (pageUrl && URL.canParse(raw) && URL.canParse(pageUrl)) {
    const target = new URL(raw);
    if (target.origin === new URL(pageUrl).origin) shown = `${target.pathname}${target.search}${target.hash}`;
  }
  return clip(shown, URL_MAX_CHARS);
}

function clip(text: string, max = NAME_MAX_CHARS): string {
  const flat = text.replace(/\s+/g, " ").trim().replace(/"/g, "'");
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}
