/**
 * CDP 无障碍树 → 给模型读的文本快照。纯函数，不碰 Electron。
 *
 * 形状对齐业界常见的 aria 快照（每行 `- role "name" [ref=eN] [状态]`，缩进表示层级），模型据 ref 指定
 * 要操作的元素。取舍：
 * - 只给可交互角色分配 ref：模型只需要能点、能填的东西有把手，其余节点只作为上下文文字出现。
 * - ref 是本次快照里的序号（`e1`、`e2`…），映射到 CDP 的 `backendDOMNodeId`。不直接用节点 id 当 ref：
 *   跨进程导航后新文档的 id 会从小重新编号，旧 ref 可能恰好指到新页面的另一个元素。映射随每次快照
 *   整体替换、导航后清空，由调用方（`browser-tab.ts`）保管。
 * - 无名的容器角色折叠掉、相邻文字合并成一行：真实页面的树里绝大多数是这类噪声。
 * - 超过字符预算就截断并说明还剩多少行，不依赖后端的输出预算去截（那会切在任意位置）。
 * - 子框架（iframe）的内容不在主框架的树里，只标出位置与数量。
 */

/** `Accessibility.getFullAXTree` 返回的节点里本模块用到的字段。 */
type AXValue = { value?: unknown };
export type AXNode = {
  nodeId: string;
  ignored?: boolean;
  role?: AXValue;
  name?: AXValue;
  value?: AXValue;
  properties?: { name: string; value: AXValue }[];
  childIds?: string[];
  parentId?: string;
  backendDOMNodeId?: number;
};

export type FormattedSnapshot = {
  text: string;
  /** `e1` → `backendDOMNodeId`。 */
  refs: Map<string, number>;
  /** 页面里未展开的子框架数量。 */
  frames: number;
};

const SNAPSHOT_CHAR_BUDGET = 12_000;
const NAME_MAX_CHARS = 200;

const INTERACTIVE_ROLES = new Set([
  "button", "checkbox", "combobox", "link", "listbox", "menuitem", "menuitemcheckbox", "menuitemradio",
  "option", "radio", "searchbox", "slider", "spinbutton", "switch", "tab", "textbox", "treeitem",
]);
/** 没有名字时不单独成行、直接展开子节点的容器角色。 */
const TRANSPARENT_ROLES = new Set(["generic", "none", "GenericContainer", "group", "LayoutTable", "LayoutTableRow", "LayoutTableCell", "Section", "paragraph", "div"]);
const SKIPPED_ROLES = new Set(["InlineTextBox", "LineBreak"]);
const TEXT_ROLES = new Set(["StaticText", "text"]);
const FRAME_ROLES = new Set(["Iframe", "IframePresentational"]);
const REPORTED_STATES = ["checked", "selected", "expanded", "disabled", "required", "pressed"] as const;

export function formatSnapshot(nodes: readonly AXNode[], budget = SNAPSHOT_CHAR_BUDGET): FormattedSnapshot {
  const byId = new Map(nodes.map((node) => [node.nodeId, node]));
  const root = nodes.find((node) => !node.parentId || !byId.has(node.parentId));
  const refs = new Map<string, number>();
  const lines: string[] = [];
  let frames = 0;

  const visit = (node: AXNode, depth: number): void => {
    const role = stringValue(node.role);
    if (SKIPPED_ROLES.has(role)) return;
    const name = clip(stringValue(node.name));
    if (FRAME_ROLES.has(role)) {
      frames += 1;
      lines.push(`${indent(depth)}- iframe${name ? ` "${name}"` : ""} (contents not shown)`);
      return;
    }
    const transparent = node.ignored || role === "RootWebArea" || (!name && TRANSPARENT_ROLES.has(role));
    if (transparent) {
      visitChildren(node, depth);
      return;
    }
    let line = `${indent(depth)}- ${role}${name ? ` "${name}"` : ""}`;
    if (INTERACTIVE_ROLES.has(role) && node.backendDOMNodeId !== undefined) {
      const ref = `e${refs.size + 1}`;
      refs.set(ref, node.backendDOMNodeId);
      line += ` [ref=${ref}]`;
    }
    line += states(node);
    const value = clip(stringValue(node.value));
    if (value && value !== name) line += ` value="${value}"`;
    lines.push(line);
    visitChildren(node, depth + 1);
  };

  const visitChildren = (node: AXNode, depth: number): void => {
    let text: string[] = [];
    const flush = () => {
      const joined = clip(text.join(" ").replace(/\s+/g, " ").trim());
      if (joined) lines.push(`${indent(depth)}- text "${joined}"`);
      text = [];
    };
    for (const childId of node.childIds ?? []) {
      const child = byId.get(childId);
      if (!child) continue;
      if (!child.ignored && TEXT_ROLES.has(stringValue(child.role))) {
        text.push(stringValue(child.name));
        continue;
      }
      flush();
      visit(child, depth);
    }
    flush();
  };

  if (root) visit(root, 0);
  return { text: withinBudget(lines, budget), refs, frames };
}

function withinBudget(lines: string[], budget: number): string {
  let used = 0;
  for (let index = 0; index < lines.length; index += 1) {
    used += lines[index].length + 1;
    if (used > budget) {
      return [...lines.slice(0, index), `… ${lines.length - index} more lines not shown`].join("\n");
    }
  }
  return lines.join("\n") || "(the page has no readable content)";
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

function stringValue(value: AXValue | undefined): string {
  const raw = value?.value;
  return typeof raw === "string" ? raw : typeof raw === "number" ? String(raw) : "";
}

function clip(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim().replace(/"/g, "'");
  return flat.length > NAME_MAX_CHARS ? `${flat.slice(0, NAME_MAX_CHARS)}…` : flat;
}

function indent(depth: number): string {
  return "  ".repeat(depth);
}
