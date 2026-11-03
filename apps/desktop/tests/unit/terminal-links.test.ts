import { Terminal } from "@xterm/xterm";
import { describe, expect, it } from "vitest";

import { findWorkspacePathCandidates, workspacePathLinks } from "@/components/terminal/terminal-links";
import type { LinkTarget } from "@/lib/links/resolve-link";

function write(terminal: Terminal, data: string): Promise<void> {
  return new Promise((resolve) => terminal.write(data, resolve));
}

async function linksOn(lines: string[], root: string, cols = 80) {
  const terminal = new Terminal({ cols, rows: 10, allowProposedApi: true });
  await write(terminal, lines.join("\r\n"));
  const opened: LinkTarget[] = [];
  const byLine = (lineNumber: number) => workspacePathLinks(terminal.buffer.active, lineNumber, root, (target) => opened.push(target));
  return { terminal, byLine, opened };
}

describe("终端里的工作区路径候选", () => {
  it("从根目录锚定，认出 POSIX 与 Windows 写法，并保留行列后缀", () => {
    expect(findWorkspacePathCandidates("see /repo/src/a.ts:12:3 now", "/repo").map((c) => c.text)).toEqual(["/repo/src/a.ts:12:3"]);
    expect(findWorkspacePathCandidates(String.raw`at C:\repo\src\a.ts:9`, String.raw`C:\repo`).map((c) => c.text)).toEqual([String.raw`C:\repo\src\a.ts:9`]);
    expect(findWorkspacePathCandidates("vite: c:/REPO/src/a.ts", String.raw`C:\repo`).map((c) => c.text)).toEqual(["c:/REPO/src/a.ts"]);
  });

  // 目的：Windows 用户名常带空格；潜在缺陷：按通用绝对路径形态找候选，在空格处截断后一个都认不出。
  it("根目录带空格时整条认出", () => {
    const root = String.raw`C:\Users\John Smith\repo`;
    expect(findWorkspacePathCandidates(String.raw`error C:\Users\John Smith\repo\src\a.ts:3`, root).map((c) => c.text))
      .toEqual([String.raw`C:\Users\John Smith\repo\src\a.ts:3`]);
  });

  // 潜在缺陷：把另一个目录里的同名段、前缀相同的兄弟目录、网址路径当成工作区路径。
  it("不认根目录前紧挨路径字符、根目录后不是分隔符的情形", () => {
    expect(findWorkspacePathCandidates("/other/repo/a.ts /repository/a.ts http://localhost:3000/repo/a", "/repo")).toEqual([]);
    expect(findWorkspacePathCandidates("src/a.ts and ./b.ts", "/repo")).toEqual([]);
  });

  // 潜在缺陷：Next.js 的 `[slug]`、`(group)` 目录被截成目录本身；句末标点与包围括号被算进路径。
  it("路径里保留成对的方括号与圆括号，剥掉句末的点与不成对的右括号", () => {
    expect(findWorkspacePathCandidates("/repo/app/[slug]/page.tsx /repo/app/(shop)/x.ts", "/repo").map((c) => c.text))
      .toEqual(["/repo/app/[slug]/page.tsx", "/repo/app/(shop)/x.ts"]);
    expect(findWorkspacePathCandidates("(see /repo/a.ts). wrote /repo/out.txt.", "/repo").map((c) => c.text))
      .toEqual(["/repo/a.ts", "/repo/out.txt"]);
  });
});

describe("工作区路径链接", () => {
  // 目的：Windows 上工具输出常用正斜杠或小写盘符；潜在缺陷：识别层按根目录字面匹配，这些都点不开。
  it("Windows 路径按解析层的规则判断归属：大小写与分隔符不敏感", async () => {
    const { byLine, terminal } = await linksOn([String.raw`error C:\Repo\src\a.ts:9`, "warn c:/repo/src/b.ts", String.raw`D:\other\x.ts`], String.raw`C:\repo`);
    expect(byLine(1).map((link) => link.text)).toEqual([String.raw`C:\Repo\src\a.ts:9`]);
    expect(byLine(2).map((link) => link.text)).toEqual(["c:/repo/src/b.ts"]);
    expect(byLine(3)).toEqual([]);
    terminal.dispose();
  });

  // 潜在缺陷：文件名里的 `#` 被当成链接片段截断，打开的是父目录。
  it("文件名里的 # 与 % 按字面交给解析层", async () => {
    const { byLine, terminal, opened } = await linksOn(["/repo/F#/x.cs /repo/a%41.ts"], "/repo");
    const links = byLine(1);
    expect(links.map((link) => link.text)).toEqual(["/repo/F#/x.cs", "/repo/a%41.ts"]);
    links[0].activate({} as MouseEvent, links[0].text);
    links[1].activate({} as MouseEvent, links[1].text);
    expect(opened).toEqual([
      { kind: "workspace", path: "F#/x.cs", directory: false },
      { kind: "workspace", path: "a%41.ts", directory: false },
    ]);
    terminal.dispose();
  });

  it("工作区外与相对路径不识别", async () => {
    const { byLine, terminal } = await linksOn(["/etc/hosts src/a.ts /repo/src/a.ts"], "/repo");
    expect(byLine(1).map((link) => link.text)).toEqual(["/repo/src/a.ts"]);
    terminal.dispose();
  });

  // 潜在缺陷：长路径在窄终端里折行后只按单行识别，点不开。
  it("折行的长路径在两行上都可点，范围跨行", async () => {
    const path = "/repo/src/components/very/long/path/file.tsx";
    const { byLine, terminal, opened } = await linksOn([`x ${path}`], "/repo", 20);
    const onFirst = byLine(1);
    const onSecond = byLine(2);
    expect(onFirst.map((link) => link.text)).toEqual([path]);
    expect(onSecond.map((link) => link.text)).toEqual([path]);
    expect(onFirst[0].range.start).toEqual({ x: 3, y: 1 });
    expect(onFirst[0].range.end.y).toBeGreaterThan(1);
    onFirst[0].activate({} as MouseEvent, path);
    expect(opened).toEqual([{ kind: "workspace", path: "src/components/very/long/path/file.tsx", directory: false }]);
    terminal.dispose();
  });

  // 潜在缺陷：行内有中文时按字符串下标定范围，可点区域整体左移。
  it("宽字符之后的路径范围按单元格计算", async () => {
    const { byLine, terminal } = await linksOn(["错误 /repo/a.ts"], "/repo");
    const [link] = byLine(1);
    // 「错误」占 4 格，加一个空格，路径从第 6 格开始。
    expect(link.range.start).toEqual({ x: 6, y: 1 });
    expect(link.range.end).toEqual({ x: 6 + "/repo/a.ts".length - 1, y: 1 });
    terminal.dispose();
  });
});
