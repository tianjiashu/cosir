import { describe, expect, it } from "vitest";

import { columnWidths, filterRows, nextSort, parseTable, sortRows } from "@/lib/workspace-files/table-parse";

const cells = (rows: { cells: string[] }[]) => rows.map((row) => row.cells);

describe("parseTable", () => {
  // 目的：引号内允许换行，大文件只读开头一段时最后一条记录可能只读到一半；潜在缺陷：表格末尾出现半条记录。
  it("截断时整条丢弃最后一条记录，引号没闭合的最后一条同样丢弃", () => {
    const text = 'name,note\nA,"多\n行"\nB,"读到一半';
    expect(cells(parseTable(text, { tsv: false, truncated: false }).rows)).toEqual([["A", "多\n行"]]);
    expect(cells(parseTable("name\nA\nB", { tsv: false, truncated: true }).rows)).toEqual([["A"]]);
    expect(cells(parseTable("name\nA\nB", { tsv: false, truncated: false }).rows)).toEqual([["A"], ["B"]]);
  });

  it("自动识别分号与制表符；.tsv 固定用制表符；表头缺列时补列名", () => {
    expect(parseTable("a;b\n1;2\n", { tsv: false, truncated: false }).header).toEqual(["a", "b"]);
    expect(parseTable("a,b\t c\n1,2\t3\n", { tsv: true, truncated: false }).header).toEqual(["a,b", "c"]);
    const ragged = parseTable("a\n1,2,3\n", { tsv: false, truncated: false });
    expect(ragged.header).toEqual(["a", "列 2", "列 3"]);
    expect(ragged.rows[0]).toEqual({ index: 1, cells: ["1", "2", "3"] });
  });

  it("数字列：千分位、小数、科学计数、百分号与空值都算数字，混入文本即不是", () => {
    const table = parseTable("n,m,t\n\"1,200\",3.5e2,x\n-4,12%,1\n,0.5,2\n", { tsv: false, truncated: false });
    expect(table.numeric).toEqual([true, true, false]);
  });

  // 潜在缺陷：尾数可省略的正则把 e5、%、E10 判成数字，文本列按数值排序全变成 NaN；单独的「-」（缺失值）让数字列退回文本。
  it("没有数字的写法不是数字；单独的「-」是缺失值，数字列照样是数字列且排在最后", () => {
    for (const value of ["e5", "E10", "%", ".", "-", "+", "e", ".e1"])
      expect(parseTable(`v\n${value}\n1\n`, { tsv: false, truncated: false }).numeric, value).toEqual(value === "-" ? [true] : [false]);
    for (const value of ["1.", ".5", "\"1,234.5\"", "-3e-2", "+7%"])
      expect(parseTable(`v\n${value}\n`, { tsv: false, truncated: false }).numeric, value).toEqual([true]);
    const withMissing = parseTable("v\n10\n-\n-2\n", { tsv: false, truncated: false });
    expect(cells(sortRows(withMissing.rows, { column: 0, direction: "asc" }, withMissing.numeric)).flat()).toEqual(["-2", "10", "-"]);
  });
});

describe("排序与筛选", () => {
  const table = parseTable("name,score\nb,10\nA,9\n,\nc,x10\n", { tsv: false, truncated: false });

  it("排序三态：升 → 降 → 还原，换列从升序开始", () => {
    expect(nextSort(null, 1)).toEqual({ column: 1, direction: "asc" });
    expect(nextSort({ column: 1, direction: "asc" }, 1)).toEqual({ column: 1, direction: "desc" });
    expect(nextSort({ column: 1, direction: "desc" }, 1)).toBeNull();
    expect(nextSort({ column: 1, direction: "desc" }, 0)).toEqual({ column: 0, direction: "asc" });
  });

  // 潜在缺陷：数字按字符串排成 10 < 9；空值在降序时跑到最前。
  it("文本列不分大小写，空值无论升降都排在最后；记录序号不变", () => {
    const asc = sortRows(table.rows, { column: 0, direction: "asc" }, table.numeric);
    expect(cells(asc).map((row) => row[0])).toEqual(["A", "b", "c", ""]);
    const desc = sortRows(table.rows, { column: 0, direction: "desc" }, table.numeric);
    expect(cells(desc).map((row) => row[0])).toEqual(["c", "b", "A", ""]);
    expect(asc[0].index).toBe(2);
    expect(sortRows(table.rows, null, table.numeric)).toBe(table.rows);
  });

  it("数字列按数值比较", () => {
    const numbers = parseTable("v\n10\n9\n100\n", { tsv: false, truncated: false });
    expect(cells(sortRows(numbers.rows, { column: 0, direction: "asc" }, numbers.numeric)).flat()).toEqual(["9", "10", "100"]);
  });

  it("筛选匹配任一单元格、不分大小写；空查询原样返回", () => {
    expect(cells(filterRows(table.rows, " a "))).toEqual([["A", "9"]]);
    expect(filterRows(table.rows, "")).toBe(table.rows);
  });

  // 潜在缺陷：按字符数估宽时一个汉字只算一格，中文列被截成「城…」。
  it("列宽按显示宽度估算（中文按两格），表头留出排序图标，限制在 6–40ch", () => {
    const widths = columnWidths(parseTable(`a,城市,b\nx,城市10,${"长".repeat(80)}\n`, { tsv: false, truncated: false }));
    expect(widths).toEqual([6, 8, 40]);
  });
});
