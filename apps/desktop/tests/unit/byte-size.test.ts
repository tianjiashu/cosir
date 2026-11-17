import { describe, expect, it } from "vitest";

import { formatByteSize } from "@/lib/format/byte-size";

// 目的：大小在胶囊、下载条、文件树里同一种写法；潜在缺陷：整数写成「318.0 KB」，或进位边界写出「1024 KB」。
describe("formatByteSize", () => {
  it("小于 10 保留一位小数且不写 .0，10 及以上取整", () => {
    expect(formatByteSize(187)).toBe("187 B");
    expect(formatByteSize(1024)).toBe("1 KB");
    expect(formatByteSize(1229)).toBe("1.2 KB");
    expect(formatByteSize(12_400)).toBe("12 KB");
    expect(formatByteSize(325_632)).toBe("318 KB");
    expect(formatByteSize(2 * 1024 * 1024)).toBe("2 MB");
    expect(formatByteSize(9.96 * 1024 * 1024)).toBe("10 MB");
    expect(formatByteSize(5 * 1024 ** 4)).toBe("5120 GB");
  });

  it("单位边界不写出 1024", () => {
    expect(formatByteSize(1023)).toBe("1023 B");
    expect(formatByteSize(1024 * 1024 - 1)).toBe("1 MB");
  });
});
