import { QUOTE_TEXT_MAX } from "@/lib/assistant/capsules";

/** 截断时追加在末尾的说明；计入长度上限。 */
const TRUNCATED_MARK = "\n…（已截断）";

/** 块里不算正文的界面元素（代码块的语言标签与复制按钮、表格与图表的工具条）。 */
const CHROME_SELECTOR = 'button, [data-streamdown$="-header"], [data-streamdown$="-actions"]';

/**
 * 一段渲染后 Markdown 的块与各块源文：对话回复由流式渲染的块登记给出（`markdown-block-sources`），
 * 文件阅读器由块级 `data-source-line` 切原文给出。块按文档顺序，互不嵌套。
 */
export type MarkdownBlocks = {
  blocks: readonly HTMLElement[];
  sourceOf: (block: HTMLElement) => string | undefined;
};

/**
 * 把渲染后 Markdown 里的选区换回 Markdown 摘录；选区里没有正文时为 null。
 *
 * 完整选中的块取源文，保留标题、列表、表格与代码的结构；两端没选全的块按类型截取——表格取选中的行并带上表头，
 * 列表取选中的项并保留编号，代码取选中的文字并保留围栏与语言，其余取选中的文字。界面元素（按钮、工具条）的文字
 * 不进摘录。超过上限（与后端同值）时截断并注明，后端仍按上限守边界。
 */
export function markdownExcerpt(range: Range, { blocks, sourceOf }: MarkdownBlocks): string | null {
  const parts = blocks
    .filter((block) => range.intersectsNode(block))
    .flatMap((block) => {
      const source = sourceOf(block);
      const text = (source !== undefined && coversAllText(block, range) ? source : partialExcerpt(block, range)).trim();
      return text ? [text] : [];
    });
  return parts.length ? truncateExcerpt(parts.join("\n\n")) : null;
}

/** 摘录原文按上限截断并注明（非 Markdown 来源，如源码与 PDF 文字，同样经过这里）。 */
export function truncateExcerpt(text: string): string {
  if (text.length <= QUOTE_TEXT_MAX) return text;
  return text.slice(0, QUOTE_TEXT_MAX - TRUNCATED_MARK.length) + TRUNCATED_MARK;
}

/** 块本身或其内部第一个匹配的元素（块的边界在两种渲染里落在不同层级：容器或元素本身）。 */
function selfOrInner<T extends Element>(block: HTMLElement, selector: string): T | null {
  return block.matches(selector) ? (block as unknown as T) : block.querySelector<T>(selector);
}

function partialExcerpt(block: HTMLElement, range: Range): string {
  const table = selfOrInner<HTMLTableElement>(block, "table");
  if (table) return tableExcerpt(table, range);
  const list = block.matches("ol, ul") ? block as HTMLOListElement | HTMLUListElement
    : block.querySelector<HTMLOListElement | HTMLUListElement>(":scope > ol, :scope > ul");
  if (list) return listExcerpt(list, range);
  const code = block.closest<HTMLElement>('[data-streamdown="code-block"]') ?? block.querySelector<HTMLElement>('[data-streamdown="code-block"]');
  if (code) return codeExcerpt(code, range);
  return selectedText(block, range);
}

/** 选区是否盖住块里第一个到最后一个正文字符（界面元素不算）；按字符判断，拖选从行首开始也算整块。 */
function coversAllText(block: HTMLElement, range: Range): boolean {
  const nodes = contentTextNodes(block);
  const first = nodes.find((node) => /\S/.test(node.data));
  const last = nodes.findLast((node) => /\S/.test(node.data));
  if (!first || !last) return false;
  return range.comparePoint(first, first.data.search(/\S/)) === 0
    && range.comparePoint(last, last.data.trimEnd().length) === 0;
}

function contentTextNodes(root: Node): Text[] {
  const nodes: Text[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => node.parentElement?.closest(CHROME_SELECTOR) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node as Text);
  return nodes;
}

/** 选区与 `element` 相交部分的正文（界面元素的文字不算）。 */
function selectedText(element: Element, range: Range): string {
  return contentTextNodes(element)
    .map((node) => {
      if (!range.intersectsNode(node)) return "";
      const start = node === range.startContainer ? range.startOffset : 0;
      const end = node === range.endContainer ? range.endOffset : node.data.length;
      return node.data.slice(start, end);
    })
    .join("");
}

function tableExcerpt(table: HTMLTableElement, range: Range): string {
  const cells = (row: HTMLTableRowElement) =>
    `| ${[...row.cells].map((cell) => (cell.textContent ?? "").trim().replaceAll("|", "\\|")).join(" | ")} |`;
  const header = table.tHead?.rows[0];
  const body = [...table.tBodies].flatMap((section) => [...section.rows]).filter((row) => range.intersectsNode(row));
  if (!header) return body.map(cells).join("\n");
  if (!body.length) return selectedText(header, range).trim();
  const divider = `|${[...header.cells].map(() => "---").join("|")}|`;
  return [cells(header), divider, ...body.map(cells)].join("\n");
}

function listExcerpt(list: HTMLOListElement | HTMLUListElement, range: Range): string {
  const ordered = list instanceof HTMLOListElement;
  const first = ordered ? list.start || 1 : 1;
  return [...list.children]
    .map((item, index) => {
      if (!range.intersectsNode(item)) return "";
      const text = selectedText(item, range).trim().replace(/\s*\n\s*/g, " ");
      return text ? `${ordered ? `${first + index}.` : "-"} ${text}` : "";
    })
    .filter(Boolean)
    .join("\n");
}

/** 代码块每行是 `pre > code` 下的一个 span，行之间没有换行字符，所以逐行截取再拼回。 */
function codeExcerpt(code: HTMLElement, range: Range): string {
  const language = code.dataset.language ?? "";
  const text = [...code.querySelectorAll("pre > code > span")]
    .filter((line) => range.intersectsNode(line))
    .map((line) => selectedText(line, range))
    .join("\n");
  return text.trim() ? `\`\`\`${language}\n${text}\n\`\`\`` : "";
}
