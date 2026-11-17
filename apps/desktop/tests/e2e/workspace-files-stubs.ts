import { readFileSync } from "node:fs";

import type { Page, Route } from "@playwright/test";

/** 桩里一个文件的现状：`missing` 表示已不存在（404），`denied` 表示无法访问（403）。 */
export type StubFile =
  | { size: number; kind?: "text" | "markdown" | "image" | "svg"; width?: number; height?: number; version?: string }
  | "missing"
  | "denied";

/**
 * 非集成 E2E 的文件接口桩：内存测试服务没有文件面板接口，而快照里有工作区内交付物时，卡片会现读 `/files/info`、
 * 会话会订阅 `/files/watch`。这里按给定表回答文件信息；监听请求挂起不回，等同一条一直开着的连接，不会断线重连、
 * 刷告警。`directories` 给出目录树（目录 → 子目录名）。图片资源回一张 1×1 PNG。需要真实读写与监听事件的用例用 `WorkspaceFilesBackend`。
 */
export async function stubWorkspaceFiles(
  page: Page,
  { files = {}, directories = {}, images = {} }: {
    files?: Record<string, StubFile>;
    directories?: Record<string, string[]>;
    /** 图片资源：工作区路径 → 本机样本文件。没给的图片回一张 1×1 PNG。 */
    images?: Record<string, string>;
  } = {},
) {
  const pending: Route[] = [];
  // 中止的监听请求（换登记集合、StrictMode 重跑 effect）不再算作连接。
  page.on("requestfailed", (request) => {
    const index = pending.findIndex((route) => route.request() === request);
    if (index >= 0) pending.splice(index, 1);
  });
  await page.route(/\/workspaces\/\d+\/files\//, async (route) => {
    const url = new URL(route.request().url());
    const operation = url.pathname.split("/").at(-1);
    const path = url.searchParams.get("path") ?? "";
    if (operation === "watch") {
      pending.push(route);
      return;
    }
    if (operation === "visibility") return route.fulfill({ json: { ignored_paths: [], revision: "empty" } });
    if (operation === "directory") {
      const children = directories[path] ?? [];
      const entries = children.map((name) => ({ path: path ? `${path}/${name}` : name, name, kind: "directory", symlink: false, hint: null }));
      return route.fulfill({ json: { path, entries, next_cursor: null, total: entries.length } });
    }
    const file = files[path];
    if (operation === "info") {
      if (file === undefined || file === "missing") return route.fulfill({ status: 404, json: { detail: { code: "FILE_NOT_FOUND", message: "文件不存在" } } });
      if (file === "denied") return route.fulfill({ status: 403, json: { detail: "无法访问文件，或链接目标位于工作区之外" } });
      const kind = file.kind ?? "text";
      return route.fulfill({
        json: {
          path, name: path.split("/").at(-1), root_path: "C:/demo", size: file.size, modified_ns: 1,
          version: file.version ?? "v1", kind, mime: kind === "image" ? "image/png" : "text/plain", encoding: kind === "image" ? null : "utf-8",
          width: file.width ?? null, height: file.height ?? null,
        },
      });
    }
    if (operation === "resource") {
      const body = images[path] ? readFileSync(images[path]) : Buffer.from(ONE_PIXEL_PNG, "base64");
      return route.fulfill({ body, contentType: "image/png" });
    }
    if (operation === "text") return route.fulfill({ json: { text: "# 报告", offset: 0, next_offset: null, size: 8 } });
    return route.fallback();
  });
  return {
    /** 当前仍开着的监听请求数（每个工作区一条）。 */
    watchCount: () => pending.length,
  };
}

const ONE_PIXEL_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
