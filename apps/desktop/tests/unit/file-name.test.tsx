import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { MiddleTruncate } from "@/components/ui/middle-truncate";
import { baseName, fileExtension } from "@/lib/files/file-name";

// 目的：「什么算扩展名」全仓一份；潜在缺陷：最后一个点之后的任意内容被当成扩展名，截断尾段没有上限撑破胶囊、
// 空态写出「FINAL DRAFT FOR REVIEW 文件」。
describe("fileExtension", () => {
  it("点不在开头、其后 1–10 位字母数字才算", () => {
    expect(fileExtension("report.md")).toBe("md");
    expect(fileExtension("archive.tar.GZ")).toBe("GZ");
    expect(fileExtension("notebook.ipynb")).toBe("ipynb");
    expect(fileExtension(".env")).toBeNull();
    expect(fileExtension("Makefile")).toBeNull();
    expect(fileExtension("报告.final draft for review")).toBeNull();
    expect(fileExtension("a.verylongextension")).toBeNull();
    expect(fileExtension("trailing.")).toBeNull();
  });
});

describe("MiddleTruncate", () => {
  /** 后段是第二个内层 span（不收缩、始终可见）。 */
  const tail = (text: string) => /<span class="shrink-0[^"]*">([^<]*)<\/span>/.exec(renderToStaticMarkup(<MiddleTruncate text={text} />))?.[1] ?? "";

  it("尾段是扩展名加 4 个字符；没有扩展名时只留 4 个字符", () => {
    expect(tail("达人投放明细_v2.csv")).toBe("细_v2.csv");
    expect(tail("报告.final draft for review")).toBe("view");
    expect(tail("Makefile")).toBe("file");
  });
});

// 目的：取文件名全仓一份。潜在缺陷：只按 `/` 切，Windows 路径整段当成文件名；目录的尾斜杠让名字变成空串。
describe("baseName", () => {
  it.each([
    ["docs/report.pdf", "report.pdf"],
    ["report.pdf", "report.pdf"],
    ["C:\\Users\\me\\report.pdf", "report.pdf"],
    ["docs/assets/", "assets"],
    ["/", "/"],
  ])("%s → %s", (path, name) => {
    expect(baseName(path)).toBe(name);
  });
});
