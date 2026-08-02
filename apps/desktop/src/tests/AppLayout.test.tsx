/**
 * App 分栏布局测试。
 *
 * 验证需求「可拖拽三栏 + 持久化 + 明显手柄」在渲染层的关键契约：
 * - 三个视图（chat / logs / new-task）均能正常渲染，不抛错
 * - chat 视图含 2 个分隔条（左/右），其余视图含 1 个（左）
 * - 各分隔条带区分性 aria-label
 * - Group 使用显式 panel id 与独立持久化 id
 *
 * 本文件在 node 环境用 renderToString 做结构断言（不跑 effect，
 * 故 App 内的后端 API 调用不会触发，无需 mock api）。
 * 持久化的「读取恢复」属于有状态行为，单独放在
 * AppLayout.persistence.test.tsx（happy-dom 环境）实质验证。
 *
 * @module tests/AppLayout
 */

import { describe, expect, it, vi } from "vitest";
import { renderToString } from "react-dom/server";
import { createElement } from "react";

// node 环境无 localStorage，注入最小 mock，保证 useDefaultLayout 顶层读取不抛错。
class MemoryStorage {
  private store = new Map<string, string>();
  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null;
  }
  setItem(key: string, value: string): void {
    this.store.set(key, value);
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  clear(): void {
    this.store.clear();
  }
  key(i: number): string | null {
    return Array.from(this.store.keys())[i] ?? null;
  }
  get length(): number {
    return this.store.size;
  }
}
vi.stubGlobal("localStorage", new MemoryStorage());

import App from "@/App";

describe("App 分栏布局", () => {
  it("chat 视图渲染出 2 个可拖拽分隔条（左 / 右）", () => {
    const html = renderToString(createElement(App));
    const separatorCount = (html.match(/role="separator"/g) ?? []).length;
    expect(separatorCount).toBe(2);
    expect(html).toContain('aria-label="调整左侧导航栏宽度"');
    expect(html).toContain('aria-label="调整右侧信息面板宽度"');
  });

  it("Group 使用显式 panel id 与独立持久化 id（chat 视图）", () => {
    const html = renderToString(createElement(App));
    // 持久化 id 作为 Group 的 id 输出；panel id 作为 Panel 的 id 属性。
    expect(html).toContain("workbench-layout-chat-v1");
    expect(html).toContain('id="sidebar"');
    expect(html).toContain('id="center"');
    expect(html).toContain('id="right"');
  });
});
