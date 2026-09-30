import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { describe, expect, it } from "vitest";
import { AssistantRuntimeProvider, useAuiState } from "@assistant-ui/react";

import type { TransportState } from "@/lib/assistant/contract";
import {
  toAddMessageCommand,
  useTaskAssistantTransportRuntime,
} from "@/lib/assistant/use-task-assistant-transport-runtime";

const INITIAL_STATE: TransportState = {
  runs: [],
  current_run_id: null,
  approvals: {},
  context_window_total: 128_000,
  error: null,
};

function TransportStateProbe() {
  const state = useAuiState((snapshot) => snapshot.thread.state);
  return createElement("output", null, state === null ? "null" : JSON.stringify(state));
}

function RuntimeHarness() {
  const runtime = useTaskAssistantTransportRuntime(501, {
    initialState: INITIAL_STATE,
    api: "http://127.0.0.1:1/assistant",
    resumeApi: "http://127.0.0.1:1/assistant/attach",
    headers: () => ({}),
    body: () => ({}),
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

    expect(html).toContain("context_window_total");
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
