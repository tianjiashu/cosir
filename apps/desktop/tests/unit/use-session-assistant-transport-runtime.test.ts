import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AssistantRuntimeProvider, useAuiState, type AssistantRuntime, type CompleteAttachment } from "@assistant-ui/react";

import type { TransportState } from "@/lib/assistant/contract";
import { SessionTransportConnection } from "@/lib/assistant/transport/session-transport-connection";
import { TransportFrameStore } from "@/lib/assistant/transport/transport-frame-store";
import {
  toAddMessageCommand,
  useSessionAssistantTransportRuntime,
} from "@/lib/assistant/transport/use-session-assistant-transport-runtime";
import { activity, transportState } from "./transport-fixtures";

const INITIAL_STATE: TransportState = transportState({
  activity: activity(1, "completed", { endReason: "stop", contextWindowTotal: 128_000 }),
});

const backend = vi.hoisted(() => ({ available: true }));
vi.mock("@/lib/backend/runtime-config", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/backend/runtime-config")>()),
  getBackendRuntimeSnapshot: () => ({ backendBaseUrl: "http://127.0.0.1:8000", generation: 1, available: backend.available }),
}));
afterEach(() => { backend.available = true; });

// 静态渲染不运行 effect，连接未 start，不会发起订阅。
const connection = () => new SessionTransportConnection({ sessionId: 501, traceId: "trace-501", surface: "conversation" });

function TransportStateProbe() {
  const state = useAuiState((snapshot) => snapshot.thread.state);
  return createElement("output", null, state === null ? "null" : JSON.stringify(state));
}

function RuntimeHarness() {
  const { runtime } = useSessionAssistantTransportRuntime(connection(), new TransportFrameStore(INITIAL_STATE));
  return createElement(
    AssistantRuntimeProvider,
    { runtime },
    createElement(TransportStateProbe),
  );
}

describe("session assistant transport runtime", () => {
  it("挂载即提供连接首帧建立的 Transport state，不经过未绑定的远程 thread", () => {
    const html = renderToStaticMarkup(createElement(RuntimeHarness));

    expect(html).toContain("contextWindowTotal");
    expect(html).toContain("128000");
    expect(html).not.toContain(">null<");
  });

  it("上传中移除最后一张图后不提交空消息，之后仍可正常输入", async () => {
    let runtime!: AssistantRuntime;
    let finishUpload!: (attachment: CompleteAttachment) => void;
    let started!: () => void;
    const uploading = new Promise<void>((resolve) => { started = resolve; });
    const submitMessage = vi.fn();
    const onError = vi.fn();
    function Harness() {
      runtime = useSessionAssistantTransportRuntime(connection(), new TransportFrameStore(INITIAL_STATE), {
        submitMessage, onError,
        adapters: { attachments: {
          accept: "image/*",
          add: async ({ file }) => ({ id: "pending", type: "image", name: file.name, file, contentType: file.type, status: { type: "requires-action", reason: "composer-send" } }),
          send: () => new Promise<CompleteAttachment>((resolve) => { finishUpload = resolve; started(); }),
          remove: async () => {},
        } },
      }).runtime;
      return null;
    }
    renderToStaticMarkup(createElement(Harness));
    await runtime.thread.composer.addAttachment(new File(["x"], "a.png", { type: "image/png" }));
    runtime.thread.composer.send();
    await uploading;
    await runtime.thread.composer.getAttachmentByIndex(0).remove();
    finishUpload({ id: "pending", type: "image", name: "a.png", contentType: "image/png", status: { type: "complete" }, content: [{ type: "image", image: `cosir-attachment://${"a".repeat(64)}` }] });
    await vi.waitFor(() => expect(runtime.thread.composer.getState().attachments).toHaveLength(0));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(submitMessage).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
    runtime.thread.composer.setText("继续提问");
    expect(runtime.thread.composer.getState().canSend).toBe(true);
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

  // 加入队列的意图借 assistant-ui 的 `steer` 穿过 composer.send：本应用没接 queue 适配器，它必须原样到这里。
  it("maps steer: false to a queue request and anything else to a plain send", () => {
    const base = { role: "user", content: [{ type: "text", text: "hi" }] };
    const command = (extra: object) => toAddMessageCommand({ ...base, ...extra } as unknown as Parameters<typeof toAddMessageCommand>[0]);
    expect(command({ steer: false }).queue).toBe(true);
    expect(command({ steer: true }).queue).toBe(false);
    expect(command({}).queue).toBe(false);
  });
});


describe("paused backend command admission", () => {
  it("returns the cleared composer draft through the current recovery callback without dispatch", async () => {
    backend.available = false;
    let runtime!: AssistantRuntime;
    const submitMessage = vi.fn();
    const onError = vi.fn((_error, commands) => {
      const text = commands[0].message.parts.find((part: { type: string }) => part.type === "text").text;
      runtime.thread.composer.setText(text);
    });
    function PausedHarness() {
      runtime = useSessionAssistantTransportRuntime(connection(), new TransportFrameStore(INITIAL_STATE), { submitMessage, onError }).runtime;
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
