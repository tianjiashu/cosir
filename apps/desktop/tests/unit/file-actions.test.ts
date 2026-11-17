import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

const electron = vi.hoisted(() => ({
  openPath: vi.fn<(target: string) => Promise<string>>(),
  showItemInFolder: vi.fn(),
  createThumbnailFromPath: vi.fn(),
}));
vi.mock("electron", () => ({
  shell: { openPath: electron.openPath, showItemInFolder: electron.showItemInFolder },
  nativeImage: { createThumbnailFromPath: electron.createThumbnailFromPath },
}));
const window = vi.hoisted(() => ({ previewFile: vi.fn(), showMainWindow: vi.fn() }));
vi.mock("@/electron/main/window", () => ({ showMainWindow: window.showMainWindow, getMainWindow: () => ({ previewFile: window.previewFile }) }));
vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent: vi.fn() }));

import { openExternally, parseExistingPath, parseThumbnailEdge, quickLook, thumbnail } from "@/electron/main/files/file-actions";
import { HostUserError } from "@/electron/main/host-user-error";

const realPlatform = process.platform;
const onPlatform = (platform: NodeJS.Platform) => Object.defineProperty(process, "platform", { value: platform });
afterEach(() => {
  onPlatform(realPlatform);
  vi.clearAllMocks();
});

// 目的：渲染进程给的路径在宿主这一处校验；潜在缺陷：对不存在的路径照样调系统，用户看到「没反应」或空白的快速查看窗口。
describe("文件出口的入参", () => {
  it("只接受存在的绝对路径，缩略图边长限制在 16–1024", () => {
    const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "file-actions-")), "a.docx");
    fs.writeFileSync(file, "x");
    expect(parseExistingPath(file)).toBe(file);
    expect(() => parseExistingPath("relative/a.docx")).toThrow(HostUserError);
    expect(() => parseExistingPath(`${file}.gone`)).toThrow("文件已不存在");
    expect(() => parseExistingPath(42)).toThrow("文件路径不合法");
    expect(parseThumbnailEdge(272)).toBe(272);
    for (const edge of [8, 2048, 1.5, "272"]) expect(() => parseThumbnailEdge(edge)).toThrow("缩略图尺寸不合法");
  });
});

describe("文件出口", () => {
  it("系统打不开时把原因告诉用户", async () => {
    electron.openPath.mockResolvedValueOnce("");
    await openExternally("/w/a.docx");
    electron.openPath.mockResolvedValueOnce("没有可以打开它的应用");
    await expect(openExternally("/w/a.xyz")).rejects.toThrow("无法用系统应用打开：没有可以打开它的应用");
  });

  // 潜在缺陷：主窗口隐藏时快速查看开在其他应用后面，用户看不到。
  it("快速查看只在 macOS 上可用，打开前先显示主窗口", () => {
    onPlatform("darwin");
    quickLook("/w/a.docx");
    expect(window.showMainWindow).toHaveBeenCalledBefore(window.previewFile);
    expect(window.previewFile).toHaveBeenCalledWith("/w/a.docx");
    onPlatform("win32");
    expect(() => quickLook("/w/a.docx")).toThrow("快速查看只在 macOS 上可用");
  });

  it("缩略图：Linux 不提供；系统给出空图时报错，否则返回 data URL", async () => {
    onPlatform("linux");
    await expect(thumbnail("/w/a.pdf", 272)).rejects.toThrow("当前系统不提供缩略图");
    onPlatform("darwin");
    electron.createThumbnailFromPath.mockResolvedValueOnce({ isEmpty: () => true });
    await expect(thumbnail("/w/a.pdf", 272)).rejects.toThrow("系统没有给出");
    electron.createThumbnailFromPath.mockResolvedValueOnce({ isEmpty: () => false, toDataURL: () => "data:image/png;base64,AA" });
    await expect(thumbnail("/w/a.pdf", 272)).resolves.toBe("data:image/png;base64,AA");
    expect(electron.createThumbnailFromPath).toHaveBeenLastCalledWith("/w/a.pdf", { width: 272, height: 272 });
  });
});
