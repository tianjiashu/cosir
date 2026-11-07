import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { AssistantRuntimeProvider, useAuiState, type AssistantRuntime } from "@assistant-ui/react";

import type { TransportState } from "@/lib/assistant/contract";
import {
  toAddMessageCommand,
  useTaskAssistantTransportRuntime,
} from "@/lib/assistant/use-task-assistant-transport-runtime";
import { activity, transportState } from "./transport-fixtures";

const INITIAL_STATE: TransportState = transportState({
  activity: activity(1, "completed", { endReason: "stop", contextWindowTotal: 128_000 }),
});

function TransportStateProbe() {
  const state = useAuiState((snapshot) => snapshot.thread.state);
  return createElement("output", null, state === null ? "null" : JSON.stringify(state));
}

function RuntimeHarness() {
  // 静态渲染不运行 effect，connectionKey 为 null 也不会发起订阅。
  const { runtime } = useTaskAssistantTransportRuntime(501, {
    initialState: INITIAL_STATE,
    traceId: "trace-501",
    connectionKey: null,
  });
  return createElement(
    AssistantRuntimeProvider,
    { runtime },
    createElement(TransportStateProbe),
  );
}

describe("task assistant transport runtime", () => {
  it("首帧直接提供 task Transport state，不经过未绑定的远程 thread", () => {
    const html = renderToStaticMarkup(createElement(RuntimeHarness));

    expect(html).toContain("contextWindowTotal");
    expect(html).toContain("128000");
    expect(html).not.toContain(">null<");
  });
});

describe("toAddMessageCommand", () => {
  it("promotes uploaded image attachment content into transport image parts", () => {
    const locator = `cosir-attachment://${"a".repeat(64)}`;
    const message = {
      role: "user",
      content: [],
      attachments: [{
        id: locator,
        type: "image",
        name: "截图.png",
        contentType: "image/png",
        content: [{ type: "image", image: locator }],
      }],
    } as unknown as Parameters<typeof toAddMessageCommand>[0];

    expect(toAddMessageCommand(message).message.parts).toEqual([
      { type: "image", image: locator },
    ]);
  });

  it("does not duplicate an image already present in message content", () => {
    const locator = `cosir-attachment://${"b".repeat(64)}`;
    const message = {
      role: "user",
      content: [{ type: "image", image: locator }],
      attachments: [{
        id: locator,
        type: "image",
        name: "截图.png",
        contentType: "image/png",
        content: [{ type: "image", image: locator }],
      }],
    } as unknown as Parameters<typeof toAddMessageCommand>[0];

    expect(toAddMessageCommand(message).message.parts).toEqual([
      { type: "image", image: locator },
    ]);
  });
});


describe("paused backend command admission", () => {
  it("returns the cleared composer draft through the current recovery callback without dispatch", async () => {
    let runtime!: AssistantRuntime;
    const submitMessage = vi.fn();
    const onError = vi.fn((_error, commands) => {
      const text = commands[0].message.parts.find((part: { type: string }) => part.type === "text").text;
      runtime.thread.composer.setText(text);
    });
    function PausedHarness() {
      runtime = useTaskAssistantTransportRuntime(501, { initialState: INITIAL_STATE, traceId: "paused", connectionKey: null, submitMessage, onError }).runtime;
      return null;
    }
    renderToStaticMarkup(createElement(PausedHarness));
    runtime.thread.composer.setText("keep the unsent draft");
    runtime.thread.composer.send();
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(submitMessage).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: "本机后端暂不可用，请稍后重试" }), [expect.objectContaining({ message: { role: "user", parts: [{ type: "text", text: "keep the unsent draft" }] } })]);
    expect(runtime.thread.composer.getState().text).toBe("keep the unsent draft");
  });
});
