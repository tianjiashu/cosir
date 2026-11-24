import type { RefRegistry } from "../ref-registry";
import { frameContentNote, isFrameNode, isFrameRole, isTextRole, stringValue, type AXNode, type SnapshotDocument } from "./document";
import { clipText } from "../text-clip";
import { chunks } from "./text-chunks";

/**
 * 读到的文档树 → 快照大纲：给模型读的一行行条目，各种视图（`views.ts`）都从它渲染。纯函数，不碰 Electron。
 *
 * 行的形状对齐业界常见的 aria 快照（`role "name" [ref=eN] [状态]`，按层级缩进，与 Playwright 的 AI 快照同一写法），
 * 模型据 ref 指定要操作的元素。取舍：
 * - 能指到就给 ref，「能不能操作」另作提示：ref 不只给点击用，悬停、拖动、滚入视野、元素截图、按键前聚焦都要指定
 *   元素，所以单独成行的元素都带 ref（文档根、语义控件内部的非交互部分除外）；折叠掉的无名容器若只装着一段文字，ref 挂在这段文字上（见
 *   `visitChildren`），`<div>悬停看说明</div>` 这类只靠脚本响应的元素因此也能指到。曾经只给「看起来能操作」的元素
 *   ref，猜不中的就只能先截图量坐标。
 * - 「看起来可点」的无语义元素（`<div onclick>` 之类：无障碍树里是无名 `generic`，只有光标样式说明它能点）单独成行
 *   并标 `[cursor=pointer]`。cursor 会继承给子元素，只认最外层那个（`pointerRoots`），否则一张卡片里每一层都会被
 *   标成可点。语义上本就可交互的角色不标。
 * - 链接带上地址（无障碍树的 `url` 属性已是绝对地址）：同源只写路径，省预算；模型可以直接导航，不必先点。
 * - ref 由调用方传入的 `RefRegistry` 分配（按节点稳定，见 `ref-registry.ts`），在这里对整棵树分配：任何视图（分页、
 *   视口内、查找、增量）里出现的 ref 都是同一套，不随视图而变。
 * - 无名的容器角色折叠掉、相邻文字合并成一行：真实页面的树里绝大多数是这类噪声。
 * - 正文不截断：它就是 Agent 要读的内容（帖子、文章），截了哪种视图都拿不回来。超长的按句读（`text-chunks.ts`）
 *   拆成相邻的几行，篇幅交给视图的分页预算，`find` 也能按行命中。名字、值与链接地址截断（名字的文字在子节点里
 *   另有一行，值与地址太长只是噪声），截断处标出还剩多少；地址截断后不能拿去导航，同一行提示改点 ref。
 * - 子框架（iframe）的内容不在上一层的树里：读到了的（`read.ts`，有上限）接在 iframe 那一行下面；没读的与读失败的
 *   只标出位置（两种说法不同），并计入数量。
 * - 每行带稳定的键（会话 + 无障碍节点 id）：增量视图按它比对前后两份大纲。合并的文字行按「所在节点 + 第几段」：
 *   文字节点常被整个替换（`textContent` 赋值、框架重新渲染），按节点认会把「改了字」报成一增一删。
 */

export type OutlineEntry = {
  /** 稳定键：同一个节点在同一文档里每次都相同（无障碍节点 id 即 DOM 节点 id，实测）；文字行见模块说明。 */
  key: string;
  depth: number;
  /** 一行的内容，不含缩进与列表符：角色、名字、ref、地址等快照语法都在里面，是给模型读的展示。 */
  text: string;
  /**
   * 这一行代表的页面文字本身（空白压成一个空格，不截断、不换引号）：元素的无障碍名字，文字行就是那段文字（拆成几行时
   * 整段记在第一行）；没有名字为空串。`wait_for` 按它判断页面上有没有某段文字，不按 `text` 里的快照语法。
   */
  name: string;
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

/** 语义上本就可交互的角色：不再另标 `[cursor=pointer]`（ref 不看它，见模块说明）。 */
const INTERACTIVE_ROLES = new Set([
  "button", "checkbox", "combobox", "link", "listbox", "menuitem", "menuitemcheckbox", "menuitemradio",
  "option", "radio", "searchbox", "slider", "spinbutton", "switch", "tab", "textbox", "treeitem",
]);
/** 没有名字时不单独成行、直接展开子节点的容器角色。 */
const TRANSPARENT_ROLES = new Set(["generic", "none", "GenericContainer", "group", "LayoutTable", "LayoutTableRow", "LayoutTableCell", "Section", "paragraph", "div"]);
const SKIPPED_ROLES = new Set(["InlineTextBox", "LineBreak"]);
/** 单独成行但不给 ref 的角色：列表符号是 `::marker` 伪元素，不会是动作目标，真实页面上却成片出现。 */
const UNADDRESSED_ROLES = new Set(["ListMarker"]);
const REPORTED_STATES = ["checked", "selected", "expanded", "disabled", "required", "pressed"] as const;

export function buildOutline(document: SnapshotDocument, refs: RefRegistry, { pageUrl }: { pageUrl?: string } = {}): Outline {
  const entries: OutlineEntry[] = [];
  let frames = 0;

  const render = (doc: SnapshotDocument, depth: number, parent: number): void => {
    const byId = new Map(doc.nodes.map((node) => [node.nodeId, node]));
    const root = doc.nodes.find((node) => !node.parentId || !byId.has(node.parentId));
    const keyOf = (node: AXNode) => `${doc.sessionId ?? ""}:${node.nodeId}`;
    const inView = (node: AXNode) => node.backendDOMNodeId !== undefined && doc.inView.has(node.backendDOMNodeId);
    const push = (key: string, text: string, name: string, depth: number, parent: number, visible: boolean): number => {
      entries.push({ key, depth, text, name, parent, visible });
      return entries.length - 1;
    };

    /** `inControl`：在语义控件（`INTERACTIVE_ROLES`）里面。控件内部的非交互部分（输入框里的值、按钮里的图标）由控件的
     * ref 代表，不另给 ref；控件里的交互元素（列表框里的选项）照常给。 */
    const visit = (node: AXNode, depth: number, parent: number, inControl: boolean): void => {
      const role = stringValue(node.role);
      if (SKIPPED_ROLES.has(role)) return;
      const fullName = collapse(stringValue(node.name));
      const name = clip(stringValue(node.name));
      if (isFrameRole(role)) {
        if (!isFrameNode(node)) return;
        const content = node.backendDOMNodeId === undefined ? undefined : doc.frames.get(node.backendDOMNodeId);
        const label = `iframe${name ? ` "${name}"` : ""}`;
        if (content) {
          render(content, depth + 1, push(keyOf(node), label, fullName, depth, parent, inView(node)));
        } else {
          frames += 1;
          push(keyOf(node), `${label} (${frameContentNote(content)})`, fullName, depth, parent, inView(node));
        }
        return;
      }
      const interactive = INTERACTIVE_ROLES.has(role);
      const clickable = !interactive && !node.ignored && node.backendDOMNodeId !== undefined && doc.pointer.has(node.backendDOMNodeId);
      const transparent = !clickable && (node.ignored || role === "RootWebArea" || (!name && TRANSPARENT_ROLES.has(role)));
      if (transparent) {
        visitChildren(node, depth, parent, inControl, role !== "RootWebArea" && !inControl);
        return;
      }
      const label = `${role}${name ? ` "${name}"` : ""}`;
      let line = label;
      if (node.backendDOMNodeId !== undefined && !UNADDRESSED_ROLES.has(role) && (interactive || !inControl)) line += ` [ref=${refOf(node.backendDOMNodeId, label)}]`;
      if (clickable) line += " [cursor=pointer]";
      line += states(node);
      const url = role === "link" ? linkUrl(node, pageUrl) : null;
      if (url) line += ` url="${url.shown}"${url.cut ? " [url cut: click this ref to open it]" : ""}`;
      const value = clip(stringValue(node.value));
      if (value && value !== name) line += ` value="${value}"`;
      visitChildren(node, depth + 1, push(keyOf(node), line, fullName, depth, parent, inView(node)), inControl || interactive);
    };

    const refOf = (backendNodeId: number, label: string) => refs.assign({ sessionId: doc.sessionId, backendNodeId }, label);

    /**
     * `folded`：`node` 是折叠掉的容器（不单独成行）。它的内容恰好只有一段文字时，这段文字的第一行带上容器的 ref——
     * 文字节点本身不是元素、不能作为动作目标，而容器的框就是这段文字的框；夹着别的元素时容器中心不在文字上，不给。
     */
    const visitChildren = (node: AXNode, depth: number, parent: number, inControl: boolean, folded = false): void => {
      let text: AXNode[] = [];
      let runs = 0;
      let firstLine = -1;
      let firstText = "";
      const before = entries.length;
      const flush = () => {
        const raw = text.map((item) => stringValue(item.name)).join(" ");
        const joined = flatten(raw);
        if (joined) {
          const key = `${keyOf(node)}/text${runs++}`;
          const visible = text.some(inView);
          if (firstLine < 0) { firstLine = entries.length; firstText = joined; }
          // 拆出的后续几行键带块号：第一块沿用原来的键，正文不长时与拆行前完全一样。
          chunks(joined).forEach((chunk, index) => push(index === 0 ? key : `${key}.${index}`, `text "${chunk}"`, index === 0 ? collapse(raw) : "", depth, parent, visible));
        }
        text = [];
      };
      for (const childId of node.childIds ?? []) {
        const child = byId.get(childId);
        if (!child) continue;
        if (!child.ignored && isTextRole(stringValue(child.role))) {
          text.push(child);
          continue;
        }
        flush();
        visit(child, depth, parent, inControl);
      }
      flush();
      const onlyText = runs === 1 && entries.slice(before).every((entry) => entry.key.startsWith(`${keyOf(node)}/text`));
      if (folded && onlyText && node.backendDOMNodeId !== undefined) {
        entries[firstLine].text += ` [ref=${refOf(node.backendDOMNodeId, `text "${clip(firstText)}"`)}]`;
      }
    };

    if (root) visit(root, depth, parent, false);
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

/** 链接地址：同源只写路径（含查询与锚点），其余写全；过长截断，`cut` 告诉调用方别让模型拿它去导航。 */
function linkUrl(node: AXNode, pageUrl: string | undefined): { shown: string; cut: boolean } | null {
  const raw = stringValue(node.properties?.find((property) => property.name === "url")?.value);
  if (!raw) return null;
  let shown = raw;
  if (pageUrl && URL.canParse(raw) && URL.canParse(pageUrl)) {
    const target = new URL(raw);
    if (target.origin === new URL(pageUrl).origin) shown = `${target.pathname}${target.search}${target.hash}`;
  }
  return { shown: clip(shown, URL_MAX_CHARS), cut: flatten(shown).length > URL_MAX_CHARS };
}

/** 压成一行。 */
function collapse(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/** 压成一行、引号换成单引号（行里的值写在双引号里）。 */
function flatten(text: string): string {
  return collapse(text).replace(/"/g, "'");
}

function clip(text: string, max = NAME_MAX_CHARS): string {
  return clipText(flatten(text), max);
}
