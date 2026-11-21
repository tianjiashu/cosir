import { useDeferredValue, useMemo, useRef, useState, type Ref } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDownIcon, ArrowUpIcon, ChevronsUpDownIcon, SearchIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { formatByteSize } from "@/lib/format/byte-size";
import { cn } from "@/lib/utils";
import {
  columnWidths,
  filterRows,
  nextSort,
  parseTable,
  sortRows,
  type ParsedTable,
  type TableSort,
} from "@/lib/workspace-files/table-parse";

const ROW_HEIGHT = 28;
/** 行号列宽（ch）：按总行数的位数估。 */
const indexWidth = (count: number) => Math.max(3, String(count).length) + 2;

/**
 * CSV / TSV 的表格视图：只读、虚拟滚动；表头吸顶，点击排序（升 → 降 → 还原）；按文本筛选；数字列右对齐、等宽数字；
 * 最左是原始记录序号，筛选排序后不变。横向滚动只在表格内发生。外框不设 overflow-hidden：那会让吸顶表头失效。
 *
 * 文本由 `TextPreview` 读好传入：2 MB 以内是整个文件；更大的文件只有开头一段（`truncated`），表格上方写明并给
 * 「以源码查看全部」。解析、截断、排序、筛选的规则都在 `table-parse`，这里只管呈现与交互。
 */
export function TablePreview({
  filterRef,
  text,
  tsv,
  truncated,
  fileSize,
  onShowSource,
}: {
  /** 筛选框：阅读器把「查找」交给它。 */
  filterRef?: Ref<HTMLInputElement>;
  text: string;
  tsv: boolean;
  truncated: boolean;
  fileSize: number;
  onShowSource: () => void;
}) {
  const table = useMemo<ParsedTable>(() => parseTable(text, { tsv, truncated }), [text, tsv, truncated]);
  const widths = useMemo(() => columnWidths(table), [table]);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<TableSort>(null);
  const deferredQuery = useDeferredValue(query);
  const visible = useMemo(
    () => sortRows(filterRows(table.rows, deferredQuery), sort, table.numeric),
    [table, deferredQuery, sort],
  );
  const scroller = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });
  const template = [`${indexWidth(table.rows.length)}ch`, ...widths.map((width) => `${width}ch`)].join(" ");
  const cell = (column: number) => cn("truncate px-2", table.numeric[column] && "text-right tabular-nums");

  return (
    <div data-slot="table-preview" className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-3 py-1.5 text-xs">
        <div className="relative w-56">
          <SearchIcon className="text-muted-foreground pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2" aria-hidden="true" />
          <Input ref={filterRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选行" aria-label="筛选行" className="h-7 pl-7 text-xs" />
        </div>
        <span className="text-muted-foreground tabular-nums" role="status">
          {deferredQuery.trim() ? `${visible.length} / ${table.rows.length} 行` : `${table.rows.length} 行`}
        </span>
        {truncated && (
          <span className="text-muted-foreground ml-auto flex items-center gap-1">
            只显示前 {table.rows.length} 行，文件共 {formatByteSize(fileSize)}
            <Button size="sm" variant="ghost" className="h-6 px-1.5 text-xs" onClick={onShowSource}>以源码查看全部</Button>
          </span>
        )}
      </div>
      <div ref={scroller} className="min-h-0 flex-1 overflow-auto p-3">
        <div role="table" aria-label="表格" aria-rowcount={visible.length + 1} className="w-max min-w-full rounded-lg border text-sm">
          <div role="row" className="bg-muted sticky top-0 z-10 grid rounded-t-[inherit] [corner-top-left-shape:inherit] [corner-top-right-shape:inherit] border-b font-medium" style={{ gridTemplateColumns: template }}>
            <span role="columnheader" className="text-muted-foreground px-2 py-1 text-right text-xs tabular-nums">#</span>
            {table.header.map((title, column) => {
              const active = sort?.column === column ? sort.direction : null;
              return (
                <button
                  key={column}
                  type="button"
                  role="columnheader"
                  aria-sort={active === "asc" ? "ascending" : active === "desc" ? "descending" : "none"}
                  title={title}
                  onClick={() => setSort(nextSort(sort, column))}
                  className={cn("hover:bg-foreground/5 flex min-w-0 items-center gap-1 px-2 py-1 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset", table.numeric[column] && "flex-row-reverse text-right")}
                >
                  <span className="truncate">{title}</span>
                  {active === "asc" ? <ArrowUpIcon className="size-3 shrink-0" aria-hidden="true" />
                    : active === "desc" ? <ArrowDownIcon className="size-3 shrink-0" aria-hidden="true" />
                      : <ChevronsUpDownIcon className="text-muted-foreground/60 size-3 shrink-0" aria-hidden="true" />}
                </button>
              );
            })}
          </div>
          <div className="relative" style={{ height: virtualizer.getTotalSize() }}>
            {virtualizer.getVirtualItems().map((item) => {
              const row = visible[item.index];
              return (
                <div
                  key={row.index}
                  role="row"
                  className="absolute inset-x-0 grid items-center border-b last:border-b-0"
                  style={{ gridTemplateColumns: template, height: ROW_HEIGHT, transform: `translateY(${item.start}px)` }}
                >
                  <span role="cell" className="text-muted-foreground px-2 text-right text-xs tabular-nums">{row.index}</span>
                  {table.header.map((_, column) => (
                    <span key={column} role="cell" className={cell(column)} title={row.cells[column]}>{row.cells[column] ?? ""}</span>
                  ))}
                </div>
              );
            })}
          </div>
        </div>
        {visible.length === 0 && <p className="text-muted-foreground p-4 text-center text-sm">{table.rows.length ? "没有匹配的行" : "表格没有数据行"}</p>}
      </div>
    </div>
  );
}
