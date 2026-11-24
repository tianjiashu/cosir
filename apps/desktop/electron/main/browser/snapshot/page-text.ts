import { frameContentNote, isFrameNode, isFrameRole, isTextRole, stringValue, type AXNode, type SnapshotDocument } from "./document";
import { chunks } from "./text-chunks";

/**
 * 读到的文档树 → 页面正文：给模型读内容用的一行行文字，与快照大纲（`outline.ts`）是同一次读取（`read.ts`）的两种
 * 投影。大纲用来找元素、做操作（带 ref），正文用来读文章、文档与搜索结果，体积约为大纲的 1/3 到 1/10（实测）。纯函数。
 *
 * 取舍：
 * - 整页、不挑主体内容：按 `article` / `main` 或 Readability 挑主体，在列表页、搜索结果页上会丢掉大半（实测有页面
 *   只剩一张卡片），篇幅交给视图的分页与 `find`（`views.ts`）。
 * - 段落按 `display` 断行：无障碍树不分行内与块级，块级元素（`SnapshotDocument.blocks`）前后断行，其余原样接在当前行上
 *   （空格只来自文字本身，代码高亮拆开的 `MyButton()` 不会多出空格），「A <b>web browser</b>, often…」才是一行。结果与浏览器的 `innerText` 基本一行对一行（实测），但多了标题与
 *   列表记号，且 iframe 的内容接在它所在的位置、不可见的内容不出现（与大纲同一套无障碍树规则）。
 * - 只留内容：标题写成 Markdown 的 `#`，列表项以 `- ` 开头，表格一行一行、单元格用 ` | ` 隔开，图片写成
 *   `[image: 替代文字]`；链接只留文字（带地址体积约翻倍，要地址就在大纲里查）；表单控件与按钮不是内容，跳过。
 *   不带 ref：要操作就读大纲。
 */

export type PageTextLine = {
  /** 给模型读的一行：带标题的 `#`、列表的 `- `、`[image: …]`、`[iframe…]` 等记号。 */
  text: string;
  /** 同一行的页面文字本身（不带上面的记号）；`wait_for` 按它判断，记号不是页面上的字。 */
  plain: string;
  /** 与视口里看得见的区域相交（行里任一段文字、标题或图片在视口里）。 */
  visible: boolean;
  /** 标题行：筛选视图据此给命中的行带上所在小节。 */
  heading: boolean;
};

export type PageText = {
  lines: PageTextLine[];
  /** 页面里没有展开的子框架数量。 */
  frames: number;
};

/**
 * 不是内容的角色：表单控件与按钮（文字是界面，不是正文），列表符号（列表项自己加 `- `）。多行文本框例外：代码编辑器、
 * 富文本编辑器里是文档内容（实测 react.dev 的示例代码在可编辑的代码框里），见 `multiline`。
 */
const SKIPPED_ROLES = new Set([
  "button", "checkbox", "combobox", "listbox", "menuitemcheckbox", "menuitemradio", "option", "radio", "searchbox",
  "slider", "spinbutton", "switch", "textbox", "MenuListPopup", "ListMarker", "InlineTextBox",
]);
const IMAGE_ROLES = new Set(["image", "img"]);
/**
 * 表格行：一行写成一行，单元格之间用「 | 」（与 `innerText` 一样一行一行），单元格里的块级元素不再断行。只认最里层的行：
 * 拿表格排版的页面（实测 Hacker News）整页套在一个外层行里，按外层行拼会把整页拼成一行。
 */
const ROW_ROLES = new Set(["row", "LayoutTableRow"]);

/** 多行文本框（`<textarea>`、可编辑区域、代码编辑器）：里面的文字按正文读。 */
function multiline(node: AXNode): boolean {
  return stringValue(node.role) === "textbox" && node.properties?.some((property) => property.name === "multiline" && property.value.value === true) === true;
}

export function buildPageText(document: SnapshotDocument): PageText {
  const lines: PageTextLine[] = [];
  let frames = 0;
  let current = "";
  let plain = "";
  let visible = false;
  /** 正在拼一个表格单元格：块级元素只隔一个空格，不断行。 */
  let inCell = false;

  const collapse = (value: string) => value.replace(/\s+/g, " ").trim();
  const flush = () => {
    const text = collapse(current);
    // 拆成几行时页面文字整段记在第一行（与大纲的文字行一样），跨块边界的一句话也能找到。
    if (text && text !== "-") chunks(text).forEach((chunk, index) => lines.push({ text: chunk, plain: index === 0 ? collapse(plain) : "", visible, heading: false }));
    current = "";
    plain = "";
    visible = false;
  };
  /**
   * 原样拼接：文字节点的名字保留了源码里的首尾空格（实测），空格只来自页面本身，不在行内元素之间另加。
   * `marker` 是这里加的记号（列表符、图片说明），只进展示、不进页面文字。
   */
  const append = (piece: string, seen: boolean, marker = false) => {
    if (!piece) return;
    current += piece;
    plain += marker ? " " : piece;
    visible ||= seen;
  };
  const lineBreak = () => (inCell ? append(" ", false) : flush());

  const render = (doc: SnapshotDocument): void => {
    const byId = new Map(doc.nodes.map((node) => [node.nodeId, node]));
    const root = doc.nodes.find((node) => !node.parentId || !byId.has(node.parentId));
    const inView = (node: AXNode) => node.backendDOMNodeId !== undefined && doc.inView.has(node.backendDOMNodeId);
    const outerRows = new Set<AXNode>();
    /** 子树里有没有表格行；装着别的行的行记进 `outerRows`。 */
    const hasRow = (node: AXNode): boolean => {
      let found = false;
      for (const childId of node.childIds ?? []) {
        const child = byId.get(childId);
        if (child && hasRow(child)) found = true;
      }
      if (found && ROW_ROLES.has(stringValue(node.role))) outerRows.add(node);
      return found || (!node.ignored && ROW_ROLES.has(stringValue(node.role)));
    };
    if (root) hasRow(root);

    const visit = (node: AXNode): void => {
      const role = stringValue(node.role);
      if (SKIPPED_ROLES.has(role) && !multiline(node)) return;
      if (role === "LineBreak") { lineBreak(); return; }
      if (!node.ignored && isTextRole(role)) { append(stringValue(node.name), inView(node)); return; }
      const name = stringValue(node.name).replace(/\s+/g, " ").trim();
      if (isFrameRole(role)) {
        if (!isFrameNode(node)) return;
        const content = node.backendDOMNodeId === undefined ? undefined : doc.frames.get(node.backendDOMNodeId);
        flush();
        if (content) render(content);
        else {
          frames += 1;
          lines.push({ text: `[iframe${name ? ` "${name}"` : ""}: ${frameContentNote(content)}]`, plain: "", visible: inView(node), heading: false });
        }
        flush();
        return;
      }
      if (!node.ignored && IMAGE_ROLES.has(role)) {
        if (name) append(` [image: ${name}] `, inView(node), true);
        return;
      }
      if (!node.ignored && role === "heading" && name) {
        // 单元格里的标题不断行，与其他内联内容同排。
        if (inCell) { append(` ${name} `, inView(node)); return; }
        flush();
        const level = node.properties?.find((property) => property.name === "level")?.value.value;
        lines.push({ text: `${"#".repeat(typeof level === "number" ? Math.min(Math.max(level, 1), 6) : 2)} ${name}`, plain: name, visible: inView(node), heading: true });
        return;
      }
      if (!node.ignored && ROW_ROLES.has(role) && !inCell && !outerRows.has(node)) {
        flush();
        const cells: string[] = [];
        const plainCells: string[] = [];
        let rowVisible = false;
        inCell = true;
        for (const childId of node.childIds ?? []) {
          const child = byId.get(childId);
          if (!child) continue;
          visit(child);
          const cell = collapse(current);
          if (cell) { cells.push(cell); plainCells.push(collapse(plain)); }
          rowVisible ||= visible;
          current = "";
          plain = "";
          visible = false;
        }
        inCell = false;
        // 单元格之间的「 | 」是记号；页面文字里单元格之间只隔一个空格。
        current = cells.join(" | ");
        plain = plainCells.join(" ");
        visible = rowVisible;
        flush();
        return;
      }
      const block = !node.ignored && node.backendDOMNodeId !== undefined && doc.blocks.has(node.backendDOMNodeId);
      if (block) lineBreak();
      if (!node.ignored && role === "listitem") append(inCell ? " " : "- ", false, true);
      for (const childId of node.childIds ?? []) {
        const child = byId.get(childId);
        if (child) visit(child);
      }
      if (block) lineBreak();
    };

    if (root) visit(root);
  };

  render(document);
  flush();
  return { lines, frames };
}
