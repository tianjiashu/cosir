// @vitest-environment happy-dom
/**
 * UI 克制风格 · 核心组件收口 的类名契约回归测试。
 *
 * 守护三条不变量：
 * 1. 非浮层组件不得出现任何 shadow* 字面类（阴影是浮层的专属语言）；
 * 2. 浮层组件 popover.tsx 必须保留 shadow-md（该留的要留住）；
 * 3. 大圆角 rounded-lg(8px) 已全部收到 rounded-md(4px)，
 *    气泡指向角 rounded-br-sm / rounded-bl-sm 属于刻意保留，不受影响。
 *
 * 断言策略：类名收口是「源码字面契约」，不是运行时行为。
 * 直接读磁盘源文件做字符串/正则断言，比 DOM 快照更稳定，
 * 也能覆盖到条件分支里、当前渲染路径打不到的类名字符串。
 * 另配少量真实渲染用例，证明纯类名清理没有破坏组件结构。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { UserMessage } from "@/components/chat/UserMessage";
import { ThinkingIndicator } from "@/components/chat/ThinkingIndicator";

/** 源码根目录（本测试文件位于 src/tests/）。 */
const SRC = resolve(__dirname, "..");

/**
 * 被扫描的非浮层组件清单：相对 src 的路径。
 * 这 13 个文件是本次改动的全部落点，必须零 shadow、零 rounded-lg。
 */
const CALM_FILES = [
  "components/ui/button.tsx",
  "components/ui/badge.tsx",
  "components/ui/input.tsx",
  "components/ui/tabs.tsx",
  "components/layout/PanelDragHandle.tsx",
  "components/layout/Sidebar.tsx",
  "pages/chat/NewTaskPage.tsx",
  "components/chat/UserMessage.tsx",
  "components/chat/AgentMessage.tsx",
  "components/chat/ThinkingIndicator.tsx",
  "components/chat/ThinkingBlock.tsx",
  "components/chat/TerminalCallCard.tsx",
  "components/chat/StatusBadge.tsx",
] as const;

/** 浮层组件：规范 §3 允许阴影，单独正向断言。 */
const OVERLAY_FILE = "components/ui/popover.tsx";

/**
 * 任意 shadow 字面类：shadow / shadow-sm / shadow-md / shadow-lg / shadow-xl /
 * shadow-none / shadow-inner 以及带前缀变体（hover:shadow-lg、dark:shadow-md）。
 * 用 (?<![\w-]) 左界防止误伤 box-shadow 之类的连字符标识；
 * 右界允许 - 后缀以便覆盖 shadow-sm 等全部尺寸。
 */
const ANY_SHADOW = /(?<![\w-])shadow(-[a-z0-9]+)?(?![\w-])/;

/**
 * rounded-lg 精确匹配：右界排除 [\w-] 使 rounded-lg 不会匹配到
 * rounded-br-sm / rounded-bl-sm（保留的气泡指向角），也不会匹配 rounded-large 之类。
 */
const ROUNDED_LG = /(?<![\w-])rounded-lg(?![\w-])/;

/**
 * 读取源文件原文。
 *
 * @param rel - 相对 src 的路径。
 * @returns UTF-8 源码字符串。
 */
function readSource(rel: string): string {
  return readFileSync(resolve(SRC, rel), "utf-8");
}

/**
 * 收集源码中命中正则的行，失败时给出「文件:行号 行内容」定位信息，
 * 避免只报一个 true/false 让人再去翻文件。
 *
 * @param source - 源码全文。
 * @param pattern - 单行匹配用的正则。
 * @returns 形如 `12: <div className="shadow-sm">` 的命中行数组。
 */
function offendingLines(source: string, pattern: RegExp): string[] {
  return source
    .split(/\r?\n/)
    .map((line, index) => ({ line, no: index + 1 }))
    .filter(({ line }) => pattern.test(line))
    .map(({ line, no }) => `${no}: ${line.trim()}`);
}

describe("UI 克制风格 · 非浮层组件零阴影", () => {
  // 目的：逐文件断言无任何 shadow 字面类。
  // 可能发现的缺陷：开发漏删某个 variant 的 shadow-sm，或 hover:shadow-lg 这类变体前缀被忽略。
  it.each(CALM_FILES)("%s 不含任何 shadow* 类", (rel) => {
    const source = readSource(rel);
    const hits = offendingLines(source, ANY_SHADOW);
    expect(hits, `${rel} 仍存在 shadow 类:\n${hits.join("\n")}`).toEqual([]);
    expect(source).not.toMatch(ANY_SHADOW);
  });

  // 目的：显式钉住四个曾带阴影的 button variant，防止回填。
  // 可能发现的缺陷：某次 shadcn 升级重新引入 shadow-sm 到 default/destructive 等 variant。
  it("button.tsx 的 default/destructive/outline/secondary variant 均无阴影", () => {
    const source = readSource("components/ui/button.tsx");
    for (const variant of ["default:", "destructive:", "outline:", "secondary:"]) {
      const line = source.split(/\r?\n/).find((l) => l.trim().startsWith(variant));
      expect(line, `button.tsx 未找到 variant 行 ${variant}`).toBeTruthy();
      expect(line as string).not.toMatch(ANY_SHADOW);
    }
  });

  // 目的：tabs active 态（data-[state=active]）不得带阴影。
  // 可能发现的缺陷：只删了静态类却漏掉 data-[state=active]:shadow 这种状态类。
  it("tabs.tsx 的 active 态不含阴影", () => {
    const source = readSource("components/ui/tabs.tsx");
    expect(source).toMatch(/data-\[state=active\]:bg-background/);
    expect(source).not.toMatch(/data-\[state=active\]:shadow/);
    expect(source).not.toMatch(ANY_SHADOW);
  });
});

describe("UI 克制风格 · 浮层阴影保留", () => {
  // 目的：证明「该留的留对了」——浮层仍有 shadow-md 提供层级感。
  // 可能发现的缺陷：批量正则清理误伤 popover，导致浮层与背景失去景深区分。
  it("popover.tsx 保留 shadow-md", () => {
    const source = readSource(OVERLAY_FILE);
    expect(source).toMatch(/(?<![\w-])shadow-md(?![\w-])/);
  });

  // 目的：浮层也只允许 shadow-md 这一档，不得升级为 shadow-lg/xl。
  // 可能发现的缺陷：为了「更明显」把浮层阴影加重，破坏克制风格上限。
  it("popover.tsx 不含比 shadow-md 更重的阴影", () => {
    const source = readSource(OVERLAY_FILE);
    expect(source).not.toMatch(/(?<![\w-])shadow-(lg|xl|2xl)(?![\w-])/);
  });
});

describe("UI 克制风格 · 大圆角收口到 4px", () => {
  // 目的：逐文件断言 rounded-lg 已清零。
  // 可能发现的缺陷：漏改某个卡片容器，造成同屏 8px/4px 圆角混用。
  it.each(CALM_FILES)("%s 不含 rounded-lg", (rel) => {
    const source = readSource(rel);
    const hits = offendingLines(source, ROUNDED_LG);
    expect(hits, `${rel} 仍存在 rounded-lg:\n${hits.join("\n")}`).toEqual([]);
  });

  // 目的：抽样确认收口方向是 rounded-md，而不是把圆角整体删光变成直角。
  // 可能发现的缺陷：清理时误删整个 rounded 类，圆角归零。
  it.each([
    "components/chat/UserMessage.tsx",
    "components/chat/ThinkingIndicator.tsx",
    "components/chat/StatusBadge.tsx",
  ])("%s 含 rounded-md", (rel) => {
    expect(readSource(rel)).toMatch(/(?<![\w-])rounded-md(?![\w-])/);
  });

  // 目的：气泡指向角 rounded-br-sm 是刻意保留的方向性设计，必须还在。
  // 可能发现的缺陷：正则清理顺手删掉指向角，气泡失去「从用户侧发出」的语义。
  it("UserMessage.tsx 保留气泡指向角 rounded-br-sm", () => {
    const source = readSource("components/chat/UserMessage.tsx");
    expect(source).toMatch(/(?<![\w-])rounded-br-sm(?![\w-])/);
  });

  // 目的：验证 ROUNDED_LG 正则本身不会误伤指向角（守护测试自身的有效性）。
  // 可能发现的缺陷：正则写成 /rounded-lg/ 无右界时，rounded-lg-foo 之类会误判；
  // 反向地，若正则写错成能匹配 rounded-br-sm，则上面的清零断言会假失败。
  it("rounded-lg 正则不匹配 rounded-br-sm / rounded-bl-sm", () => {
    expect(ROUNDED_LG.test("rounded-br-sm rounded-bl-sm rounded-md")).toBe(false);
    expect(ROUNDED_LG.test("rounded-lg")).toBe(true);
  });

  // 目的：验证 ANY_SHADOW 正则确实能抓到各档阴影与变体前缀（守护测试自身的有效性）。
  // 可能发现的缺陷：正则漏掉 hover:/dark: 前缀或 shadow 裸类，导致清零断言形同虚设。
  it("shadow 正则覆盖裸类/尺寸/变体前缀", () => {
    for (const s of ["shadow", "shadow-sm", "shadow-md", "shadow-lg", "hover:shadow-lg", "dark:shadow-md"]) {
      expect(ANY_SHADOW.test(s), `未匹配 ${s}`).toBe(true);
    }
    expect(ANY_SHADOW.test("bg-primary rounded-md px-4")).toBe(false);
  });
});

describe("UI 克制风格 · 渲染无回归", () => {
  // 目的：Button 纯类名清理后仍正常渲染 button 元素并保留圆角与主色。
  // 可能发现的缺陷：删类名时误删逗号/引号破坏 cva 配置，导致 className 为空或抛错。
  it("Button 渲染出 button 元素且类名含 rounded-md、无 shadow", () => {
    const { getByRole, unmount } = render(<Button>提交</Button>);
    const btn = getByRole("button", { name: "提交" });
    expect(btn.tagName).toBe("BUTTON");
    expect(btn.className).toMatch(/(?<![\w-])rounded-md(?![\w-])/);
    expect(btn.className).toMatch(/bg-primary/);
    expect(btn.className).not.toMatch(ANY_SHADOW);
    unmount();
  });

  // 目的：覆盖 destructive/outline/secondary 三个曾带阴影的 variant 的实际产出类名。
  // 可能发现的缺陷：某 variant 的 cva 值被改坏，运行时仍注入 shadow。
  it.each(["destructive", "outline", "secondary"] as const)(
    "Button variant=%s 渲染类名无 shadow",
    (variant) => {
      const { getByRole, unmount } = render(<Button variant={variant}>操作</Button>);
      expect(getByRole("button").className).not.toMatch(ANY_SHADOW);
      unmount();
    },
  );

  // 目的：Badge default/destructive 渲染后无阴影且为 4px 圆角。
  // 可能发现的缺陷：badge cva 基类被误改，圆角丢失或阴影残留。
  it.each(["default", "destructive"] as const)("Badge variant=%s 无 shadow 且含 rounded-md", (variant) => {
    const { container, unmount } = render(<Badge variant={variant}>运行中</Badge>);
    const el = container.firstElementChild as HTMLElement;
    expect(el).toBeTruthy();
    expect(el.textContent).toBe("运行中");
    expect(el.className).toMatch(/(?<![\w-])rounded-md(?![\w-])/);
    expect(el.className).not.toMatch(ANY_SHADOW);
    unmount();
  });

  // 目的：Input 渲染为 input 元素，圆角 4px 且无阴影，placeholder 等原生属性透传未坏。
  // 可能发现的缺陷：删 shadow-sm 时破坏 cn() 调用，className 拼接异常或属性透传丢失。
  it("Input 渲染无 shadow 且原生属性透传正常", () => {
    const { getByPlaceholderText, unmount } = render(<Input placeholder="输入任务" />);
    const input = getByPlaceholderText("输入任务") as HTMLInputElement;
    expect(input.tagName).toBe("INPUT");
    expect(input.className).toMatch(/(?<![\w-])rounded-md(?![\w-])/);
    expect(input.className).not.toMatch(ANY_SHADOW);
    unmount();
  });

  // 目的：UserMessage 气泡渲染后同时具备 rounded-md 与保留的 rounded-br-sm，且无阴影。
  // 可能发现的缺陷：气泡类名串改动破坏指向角或注入阴影；内容渲染结构被打乱。
  it("UserMessage 气泡含 rounded-md + rounded-br-sm 且无 shadow", () => {
    const { container, getByText, unmount } = render(<UserMessage content="你好" />);
    const text = getByText("你好");
    expect(text).toBeTruthy();
    // 气泡是 <p> 的父元素；container 本身是 div，用 "div > div" 会先命中外层 flex 容器。
    const bubble = text.parentElement as HTMLElement;
    expect(bubble).toBeTruthy();
    expect(container.firstElementChild?.className).toMatch(/justify-end/);
    expect(bubble.className).toMatch(/(?<![\w-])rounded-md(?![\w-])/);
    expect(bubble.className).toMatch(/(?<![\w-])rounded-br-sm(?![\w-])/);
    expect(bubble.className).not.toMatch(ANY_SHADOW);
    unmount();
  });

  // 目的：UserMessage 空内容边界——纯空白应渲染 null，类名清理不应改变这一短路逻辑。
  // 可能发现的缺陷：改样式时误动 trim 判空分支，导致空气泡出现在时间线上。
  it("UserMessage 空白内容渲染为空", () => {
    // 注意：JSX 属性字面量不解析 \n 转义，必须用表达式容器传入真实换行符。
    const { container, unmount } = render(<UserMessage content={"   \n  \t "} />);
    expect(container.innerHTML).toBe("");
    unmount();
  });

  // 目的：ThinkingIndicator 结构（容器 + 三个跳动圆点）与 4px 圆角未受影响。
  // 可能发现的缺陷：把圆点的 rounded-full 一起收成 rounded-md，圆点变方块。
  it("ThinkingIndicator 结构完整且圆点保持 rounded-full", () => {
    const { getByTestId, unmount } = render(<ThinkingIndicator />);
    const root = getByTestId("thinking-indicator");
    expect(root).toBeTruthy();
    const inner = root.firstElementChild as HTMLElement;
    expect(inner.className).toMatch(/(?<![\w-])rounded-md(?![\w-])/);
    expect(inner.className).not.toMatch(ANY_SHADOW);
    const dots = getByTestId("thinking-dots").querySelectorAll("span");
    expect(dots.length).toBe(3);
    dots.forEach((dot) => {
      expect(dot.className).toMatch(/(?<![\w-])rounded-full(?![\w-])/);
    });
    unmount();
  });
});

describe("UI 克制风格 · 全量兜底扫描", () => {
  // 目的：跨文件兜底——13 个文件合并后整体零 shadow，防止清单遗漏单文件时被逐项用例放过。
  // 可能发现的缺陷：清单本身写漏，或新增 variant 时带回阴影。
  it("13 个非浮层文件合计零 shadow 命中", () => {
    const report = CALM_FILES.flatMap((rel) =>
      offendingLines(readSource(rel), ANY_SHADOW).map((l) => `${rel} ${l}`),
    );
    expect(report, `仍有 shadow 残留:\n${report.join("\n")}`).toEqual([]);
  });

  // 目的：跨文件兜底——13 个文件合并后整体零 rounded-lg。
  // 可能发现的缺陷：某文件在条件分支字符串里藏着 rounded-lg。
  it("13 个非浮层文件合计零 rounded-lg 命中", () => {
    const report = CALM_FILES.flatMap((rel) =>
      offendingLines(readSource(rel), ROUNDED_LG).map((l) => `${rel} ${l}`),
    );
    expect(report, `仍有 rounded-lg 残留:\n${report.join("\n")}`).toEqual([]);
  });
});
