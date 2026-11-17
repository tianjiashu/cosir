import Papa from "papaparse";

/** 一行数据与它在文件里的记录序号（1 起算、不含表头）；排序与筛选后序号不变，用来对照原文。 */
export type TableRow = { index: number; cells: string[] };

export type ParsedTable = {
  header: string[];
  rows: TableRow[];
  /** 每列是否按数字处理（右对齐、按数值排序）。 */
  numeric: boolean[];
};

export type SortDirection = "asc" | "desc";
export type TableSort = { column: number; direction: SortDirection } | null;

/** 判定数字列时每列最多看多少个非空值：上万行时全量检查没有必要。 */
const NUMERIC_SAMPLE = 200;
/** 一个数字（千分位、小数、科学计数、百分号）：尾数里至少有一位数字，`e5`、`%`、`.` 这类不算。 */
const NUMBER = /^[-+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?%?$/i;
/** 表格里常见的「没有值」的写法：和空单元格一样不参与数字列判定，排序时排在最后。 */
const MISSING = new Set(["", "-"]);

/**
 * 解析 CSV / TSV 文本。`.tsv` 固定按制表符，其余自动识别分隔符（papaparse 在逗号、制表符、竖线、分号等里采样）。
 *
 * `truncated` 表示文本只是文件开头一段（大文件分段读取）：最后一条记录可能只读到一半，整条丢弃。另外，
 * 最后一条记录的引号没闭合（papaparse 报 `MissingQuotes`）同样丢弃——引号内允许换行，不能按行截断。截断规则
 * 只看这两点、不依赖 papaparse 报告的偏移（`meta.cursor` 在部分模式下不准）。
 */
export function parseTable(text: string, options: { tsv: boolean; truncated: boolean }): ParsedTable {
  const result = Papa.parse<string[]>(text, { delimiter: options.tsv ? "\t" : "", skipEmptyLines: true });
  const records = result.data;
  const last = records.length - 1;
  const unclosed = result.errors.some((error) => error.code === "MissingQuotes" && (error.row === undefined || error.row >= last));
  if (records.length > 1 && (options.truncated || unclosed)) records.pop();
  const width = records.reduce((max, record) => Math.max(max, record.length), 0);
  const [first = [], ...body] = records;
  const header = Array.from({ length: width }, (_, column) => first[column]?.trim() || `列 ${column + 1}`);
  const rows = body.map((cells, offset) => ({ index: offset + 1, cells }));
  return { header, rows, numeric: header.map((_, column) => isNumericColumn(rows, column)) };
}

function isNumericColumn(rows: TableRow[], column: number): boolean {
  let seen = 0;
  for (const row of rows) {
    const value = row.cells[column]?.trim() ?? "";
    if (MISSING.has(value)) continue;
    if (!NUMBER.test(value)) return false;
    if (++seen >= NUMERIC_SAMPLE) break;
  }
  return seen > 0;
}

function numberOf(value: string | undefined): number {
  const cleaned = value?.trim().replace(/,/g, "").replace(/%$/, "");
  return cleaned ? Number(cleaned) : Number.NaN;
}

/** 排序的下一态：升 → 降 → 还原（点别的列从升序开始）。 */
export function nextSort(current: TableSort, column: number): TableSort {
  if (current?.column !== column) return { column, direction: "asc" };
  return current.direction === "asc" ? { column, direction: "desc" } : null;
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

/**
 * 按列排序，返回新数组。数字列按数值比较，空值与非数字排在最后（无论升降）；文本列用数字感知的本地化比较。
 * 先为每行算好排序键再排，比较函数里不反复解析，十万行级别也不会明显卡顿。
 */
export function sortRows(rows: TableRow[], sort: TableSort, numeric: boolean[]): TableRow[] {
  if (!sort) return rows;
  const { column, direction } = sort;
  const sign = direction === "asc" ? 1 : -1;
  if (numeric[column]) {
    const keyed = rows.map((row) => ({ row, key: numberOf(row.cells[column]) }));
    keyed.sort((a, b) => {
      const aMissing = Number.isNaN(a.key), bMissing = Number.isNaN(b.key);
      if (aMissing || bMissing) return aMissing === bMissing ? a.row.index - b.row.index : aMissing ? 1 : -1;
      return (a.key - b.key) * sign || a.row.index - b.row.index;
    });
    return keyed.map(({ row }) => row);
  }
  const keyed = rows.map((row) => ({ row, key: row.cells[column]?.trim() ?? "" }));
  keyed.sort((a, b) => {
    if (!a.key || !b.key) return !a.key === !b.key ? a.row.index - b.row.index : !a.key ? 1 : -1;
    return collator.compare(a.key, b.key) * sign || a.row.index - b.row.index;
  });
  return keyed.map(({ row }) => row);
}

/** 按文本筛选：任一单元格包含查询词即保留（不区分大小写）；空查询返回原数组。 */
export function filterRows(rows: TableRow[], query: string): TableRow[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return rows;
  return rows.filter((row) => row.cells.some((cell) => cell.toLocaleLowerCase().includes(needle)));
}

/** 中日韩文字与全角符号占两个半角宽度（`ch` 按半角数字计）。 */
const WIDE = /[\u1100-\u115f\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]/u;

function displayWidth(text: string): number {
  let width = 0;
  for (const char of text) width += WIDE.test(char) ? 2 : 1;
  return width;
}

/**
 * 列宽（单位 ch）：按表头与前若干行的显示宽度估算（中文按两个半角计），限制在可读范围内；不逐格测量文字宽度。
 * 表头额外留出排序图标的位置。
 */
export function columnWidths(table: ParsedTable, sample = 100): number[] {
  return table.header.map((title, column) => {
    let widest = displayWidth(title) + 2;
    for (const row of table.rows.slice(0, sample)) widest = Math.max(widest, displayWidth(row.cells[column] ?? ""));
    return Math.min(40, Math.max(6, widest + 2));
  });
}
