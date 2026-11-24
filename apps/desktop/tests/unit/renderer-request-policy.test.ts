import { beforeEach, describe, expect, it, vi } from "vitest";

const { logDesktopEvent } = vi.hoisted(() => ({ logDesktopEvent: vi.fn() }));
vi.mock("@/electron/main/desktop-log", () => ({ logDesktopEvent }));

import { CAPABILITY_HEADER, RendererRequestPolicy } from "@/electron/main/renderer-request-policy";

const WINDOW = 7;
const identity = { baseUrl: "http://127.0.0.1:8765", token: "t", generation: 1 };
const policy = () => new RendererRequestPolicy(() => identity, () => WINDOW, "app://cosir");
let nextId = 100;
// 同一个 id 代表同一条请求（重定向），每次新请求取新 id。
const fromWindow = (url: string, id = nextId++) => ({
  id, url, webContentsId: WINDOW, frame: { url: "app://cosir/index.html", parent: null }, initiatorOrigin: "app://cosir",
});

beforeEach(() => logDesktopEvent.mockClear());

describe("RendererRequestPolicy", () => {
  // 目的：界面渲染模型写的内容不会连到公网；潜在缺陷：链接图标、Markdown 图片一显示就把主机名与参数交给对方站点。
  it("blocks the app's pages from reaching anything but loopback, and logs it", () => {
    const rules = policy();
    for (const url of ["https://x.attacker.example/favicon.ico", "http://192.168.1.2/a.png", "wss://example.com/s"]) {
      expect(rules.begin(fromWindow(url))).toBe(false);
    }
    expect(rules.begin({ id: 2, url: "https://example.com/a", webContentsId: 99 })).toBe(false);
    expect(logDesktopEvent).toHaveBeenCalledWith("WARNING", "renderer_request_policy", "renderer_outbound_blocked", { origin: "https://x.attacker.example" });
    for (const url of ["http://localhost:5173/src/main.tsx", "ws://localhost:5173/", "http://127.0.0.1:8765/health", "http://[::1]:9/"]) {
      expect(rules.begin(fromWindow(url))).toBe(true);
    }
  });

  it("leaves the main process's own requests alone", () => {
    expect(policy().begin({ id: 3, url: "https://example.com/update" })).toBe(true);
  });

  // 目的：出站规则不改变后端身份；潜在缺陷：重定向跳出去或别的页面拿到 capability。
  it("still gives the backend capability only to the main window's direct requests", () => {
    const rules = policy();
    const request = fromWindow("http://127.0.0.1:8765/sessions", 4);
    expect(rules.begin(request)).toBe(true);
    expect(rules.headers(request, {})[CAPABILITY_HEADER]).toBe("t");
    expect(rules.begin({ ...request, url: "https://example.com/stolen" })).toBe(false);
  });
});
