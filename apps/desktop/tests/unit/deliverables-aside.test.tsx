import { QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

// 服务端渲染读 zustand 的初始状态；卡片只经 useSessionDeliverables 读，这里改成直接读当前状态。
vi.mock("@/lib/deliverables/deliverables-store", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/deliverables/deliverables-store")>();
  return { ...actual, useSessionDeliverables: (sessionId: number | null) => (sessionId === null ? [] : actual.useDeliverablesStore.getState().sessions[sessionId] ?? []) };
});

import { DeliverablesBody, DeliverablesSummary, useDeliverablesHasContent } from "@/components/conversation-aside/deliverables-card";
import { parseTransportState } from "@/lib/assistant/transport/snapshot-validation";
import { useDeliverablesStore } from "@/lib/deliverables/deliverables-store";
import { createQueryClient } from "@/lib/query/query-client";
import { deliveredFile, deliveredUrl, transportState } from "./transport-fixtures";

const render = (node: React.ReactNode) => renderToStaticMarkup(<QueryClientProvider client={createQueryClient()}>{node}</QueryClientProvider>);
afterEach(() => useDeliverablesStore.setState({ sessions: {} }));

// 目的：侧栏只镜像后端的会话清单；潜在缺陷：离开会话后按过期清单渲染，或删除后留着旧桶。
describe("useDeliverablesStore", () => {
  it("同步、离开时置空、删除后丢弃", () => {
    const list = [deliveredFile("a.md")];
    useDeliverablesStore.getState().sync(7, list);
    expect(useDeliverablesStore.getState().sessions[7]).toBe(list);
    useDeliverablesStore.getState().sync(7, null);
    expect(useDeliverablesStore.getState().sessions[7]).toBeNull();
    useDeliverablesStore.getState().forget([7]);
    expect(useDeliverablesStore.getState().sessions).toEqual({});
  });
});

describe("产出卡", () => {
  it("有交付物才有内容；折叠摘要是条数；超过五条收进「显示更多」", () => {
    function HasContent() {
      return <>{String(useDeliverablesHasContent(7))}</>;
    }
    expect(render(<HasContent />)).toBe("false");
    const list = [...Array.from({ length: 6 }, (_, i) => deliveredFile(`f${i}.md`)), deliveredUrl("https://example.com/r")];
    useDeliverablesStore.getState().sync(7, list);
    expect(render(<HasContent />)).toBe("true");
    expect(render(<DeliverablesSummary sessionId={7} />)).toBe("7");
    const body = render(<DeliverablesBody sessionId={7} />);
    expect(body.match(/data-slot="aside-deliverable"/g)).toHaveLength(5);
    expect(body).toContain("显示更多（2）");
    expect(body).toContain("f0.md");
    expect(body).not.toContain("f5.md");
  });
});

// 潜在缺陷：后端漏发清单或清单里有重复 key，前端照常渲染出两条。
describe("快照顶层 deliverables", () => {
  it("必填、逐项校验、key 不重复", () => {
    expect(parseTransportState(transportState({ deliverables: [deliveredFile("a.md")] })).deliverables).toHaveLength(1);
    const missing: Record<string, unknown> = { ...transportState() };
    delete missing.deliverables;
    expect(() => parseTransportState(missing)).toThrow("snapshot");
    expect(() => parseTransportState(transportState({ deliverables: [deliveredFile("a.md"), deliveredFile("a.md")] }))).toThrow("key 不重复");
    expect(() => parseTransportState(transportState({ deliverables: [{ ...deliveredFile("a.md"), size: "1" } as never] }))).toThrow("deliverables[0]");
  });
});
