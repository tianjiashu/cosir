import { describe, expect, it } from "vitest";

import { findMatches } from "@/lib/find/find-matches";

describe("findMatches", () => {
  it("不区分大小写，按原文位置返回不重叠的命中", () => {
    expect(findMatches("Budget budget BUDGET", "budget", 10)).toEqual([
      { start: 0, end: 6 }, { start: 7, end: 13 }, { start: 14, end: 20 },
    ]);
    expect(findMatches("aaaa", "aa", 10)).toEqual([{ start: 0, end: 2 }, { start: 2, end: 4 }]);
  });

  it("小写后长度会变的字符（İ）不让后面的位置错开", () => {
    const text = "İstanbul – budget";
    const [match] = findMatches(text, "budget", 10);
    expect(match).toEqual({ start: 11, end: 17 });
    expect(text.slice(match.start, match.end)).toBe("budget");
    expect(findMatches(text, "İSTAN", 10)).toEqual([{ start: 0, end: 5 }]);
  });

  it("查找词按字面匹配，正则元字符不生效；达到上限即停", () => {
    expect(findMatches("a.b axb (c)", "a.b", 10)).toEqual([{ start: 0, end: 3 }]);
    expect(findMatches("(c)", "(c)", 10)).toEqual([{ start: 0, end: 3 }]);
    expect(findMatches("x x x", "x", 2)).toHaveLength(2);
  });
});
