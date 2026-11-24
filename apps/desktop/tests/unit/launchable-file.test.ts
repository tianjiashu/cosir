import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { isLaunchable } from "@/electron/main/files/launchable-file";

let dir = "";
const file = (name: string, mode = 0o644) => {
  const target = path.join(dir, name);
  fs.writeFileSync(target, "x");
  fs.chmodSync(target, mode);
  return target;
};

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "launchable-"));
});
afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

// 目的：认出交给系统打开就会运行的文件；潜在缺陷：模型写的脚本作为交付物，用户点「打开」就运行了。
describe("isLaunchable", () => {
  it("knows each platform's programs, scripts and shortcuts, and leaves documents alone", () => {
    expect(isLaunchable(file("run.command"), "darwin")).toBe(true);
    expect(isLaunchable(file("Setup.PKG"), "darwin")).toBe(true);
    expect(isLaunchable(file("go.bat"), "win32")).toBe(true);
    expect(isLaunchable(file("link.lnk"), "win32")).toBe(true);
    expect(isLaunchable(file("app.desktop"), "linux")).toBe(true);
    for (const platform of ["darwin", "win32", "linux"]) {
      expect(isLaunchable(file("report.pdf"), platform)).toBe(false);
    }
  });

  it("treats an app bundle directory as launchable", () => {
    const bundle = path.join(dir, "Tool.app");
    fs.mkdirSync(bundle);
    expect(isLaunchable(bundle, "darwin")).toBe(true);
  });

  it("flags executable files without an extension only where the system would run them", () => {
    const binary = file("build", 0o755);
    expect(isLaunchable(binary, "darwin")).toBe(true);
    expect(isLaunchable(binary, "linux")).toBe(true);
    expect(isLaunchable(file("notes", 0o644), "darwin")).toBe(false);
    // 有后缀时系统按后缀选应用：带执行权限的 .py 在编辑器里打开。
    expect(isLaunchable(file("tool.py", 0o755), "darwin")).toBe(false);
  });

  it("looks through symbolic links to what they point at", () => {
    const disguised = path.join(dir, "report.pdf");
    fs.symlinkSync(file("payload.command"), disguised);
    expect(isLaunchable(disguised, "darwin")).toBe(true);
  });
});
