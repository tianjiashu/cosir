import { readFileSync } from "node:fs";
import path from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { describeDeliverable } from "@/components/deliverables/deliverable-label";
import { DeliverablesCard } from "@/components/conversation/tools/reply-presentation/deliverables-card";
import { createQueryClient } from "@/lib/query/query-client";
import { toolCallPart } from "./transport-fixtures";

/** 与后端 `DeliveredUrl` 共用的样本表：后端收下的（accepted）前端必须解析得了。 */
const samples = JSON.parse(readFileSync(path.resolve(__dirname, "../../../backend/tests/fixtures/deliverable_urls.json"), "utf-8")) as { accepted: string[] };

const url = (value: string) => ({ type: "url" as const, key: value, caption: null, url: value });

// 目的：交付网址的合法规则只在后端一处，前端信任它直接 new URL；潜在缺陷：两边规则一宽一严，后端收下的地址让整组卡片渲染抛错。
describe("交付网址的前后端契约", () => {
  it.each(samples.accepted)("后端收下的 %s 前端能解析、能渲染", (value) => {
    expect(() => new URL(value)).not.toThrow();
    expect(describeDeliverable(url(value)).meta).not.toBe("");
    const card = { kind: "deliverables" as const, index: 0, part: toolCallPart("c", "present_deliverables"), note: null, items: [{ deliverable: url(value), toolCallId: "c" }] };
    expect(() => renderToStaticMarkup(<QueryClientProvider client={createQueryClient()}><DeliverablesCard sessionId={1} card={card} /></QueryClientProvider>)).not.toThrow();
  });
});
