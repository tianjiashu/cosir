import { describe, expect, it } from "vitest";

import type { TransportMessage, TransportRun, TransportState, TransportToolCallPart } from "@/lib/assistant/contract";
import { toTransportThreadView } from "@/lib/assistant/converter";
import { createTransportViewConverter } from "@/lib/assistant/transport-view-converter";

const message = (id: string, role: TransportMessage["role"], text: string): TransportMessage => ({
  id,
  role,
  parts: [{ type: "text", text, status: role === "assistant" ? "running" : undefined }],
});

const run = (runId: number, messages: TransportMessage[], status = "running"): TransportRun => ({
  runId,
  status,
  endReason: null,
  messages,
  usage: null,
  error: null,
});

const state = (runs: TransportRun[], error: TransportState["error"] = null): TransportState => ({
  runs,
  current_run_id: runs.at(-1)?.runId ?? null,
  approvals: {},
  context_usage_ratio: null,
  context_usage_used: null,
  context_window_total: null,
  error,
});

const metadata = (pendingCommands: readonly unknown[] = [], isSending = false) => ({
  pendingCommands,
  isSending,
});

const toolMessage = (status: TransportToolCallPart["status"]): TransportMessage => ({
  id: "tool-message",
  role: "assistant",
  parts: [{
    type: "tool-call",
    toolCallId: "tool-1",
    toolName: "execute_terminal",
    args: { command: "npm test" },
    status,
  }],
});

const withoutCreatedAt = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(withoutCreatedAt);
  if (typeof value !== "object" || value === null) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => key !== "createdAt")
      .map(([key, nested]) => [key, withoutCreatedAt(nested)]),
  );
};

describe("transport view converter cache", () => {
  it("reuses unchanged canonical messages when the active message streams", () => {
    const convert = createTransportViewConverter();
    const historical = message("m-1", "assistant", "historical");
    const active = message("m-2", "assistant", "partial");
    const first = convert(state([run(1, [historical, active])]), metadata());
    const activeUpdate = { ...active, parts: [{ type: "text" as const, text: "partial update", status: "running" as const }] };
    const second = convert(state([run(1, [historical, activeUpdate])]), metadata());

    expect(second.messages[0]).toBe(first.messages[0]);
    expect(second.messages[1]).not.toBe(first.messages[1]);
    expect(second.messages[1]).toMatchObject({ id: "m-2", content: [{ type: "text", text: "partial update" }] });
  });

  it("invalidates only messages whose Run status context changed", () => {
    const convert = createTransportViewConverter();
    const firstRunMessage = message("m-1", "assistant", "first");
    const secondRunMessage = message("m-2", "assistant", "second");
    const first = convert(state([run(1, [firstRunMessage]), run(2, [secondRunMessage])]), metadata());
    const second = convert(state([
      run(1, [firstRunMessage], "completed"),
      run(2, [secondRunMessage]),
    ]), metadata());

    expect(second.messages[0]).not.toBe(first.messages[0]);
    expect(second.messages[1]).toBe(first.messages[1]);
    expect(second.messages[0]).toMatchObject({ status: { type: "complete" } });
  });

  it("invalidates the previous last assistant message when a new one is appended", () => {
    const convert = createTransportViewConverter();
    const previous = message("m-1", "assistant", "first");
    const first = convert(state([run(1, [previous])]), metadata());
    const next = message("m-2", "assistant", "second");
    const second = convert(state([run(1, [previous, next])]), metadata());

    expect(second.messages[0]).not.toBe(first.messages[0]);
    expect(second.messages[1]).toMatchObject({ id: "m-2" });
    expect(second.messages[0].metadata?.custom).toMatchObject({ isLastRunMessage: false });
    expect(second.messages[1].metadata?.custom).toMatchObject({ isLastRunMessage: true });
  });

  it("caches pending messages by identity and their display semantics", () => {
    const convert = createTransportViewConverter();
    const command = {
      type: "add-message",
      sourceId: "source-1",
      parentId: null,
      message: { role: "user" as const, parts: [{ type: "text", text: "pending" }] },
    };
    const first = convert(state([]), metadata([command]));
    const second = convert(state([]), metadata([command]));
    expect(second.messages[0]).toBe(first.messages[0]);

    command.message.parts[0].text = "changed";
    const third = convert(state([]), metadata([command]));
    expect(third.messages[0]).not.toBe(second.messages[0]);
    expect(third.messages[0]).toMatchObject({ content: [{ type: "text", text: "changed" }] });
  });

  it("caches and invalidates snapshot error messages without touching canonical messages", () => {
    const convert = createTransportViewConverter();
    const canonical = message("m-1", "assistant", "stable");
    const error = { code: "MODEL_SELECTION_REQUIRED", message: "请先选择模型" };
    const first = convert(state([run(1, [canonical])], error), metadata());
    const second = convert(state([run(1, [canonical])], error), metadata());

    expect(second.messages[0]).toBe(first.messages[0]);
    expect(second.messages[1]).toBe(first.messages[1]);

    const cleared = convert(state([run(1, [canonical])]), metadata());
    expect(cleared.messages).toHaveLength(1);
    expect(cleared.messages[0]).toBe(first.messages[0]);
  });

  it("preserves tool part references until the backend tool part changes", () => {
    const convert = createTransportViewConverter();
    const pending = toolMessage("pending");
    const first = convert(state([run(1, [pending])]), metadata());
    const sameMetadata = convert(state([run(1, [pending])]), metadata([], false));
    expect(sameMetadata.messages[0]).toBe(first.messages[0]);
    expect(sameMetadata.messages[0].content[0]).toBe(first.messages[0].content[0]);

    const completed = toolMessage("completed");
    const third = convert(state([run(1, [completed])]), metadata());
    expect(third.messages[0]).not.toBe(first.messages[0]);
    expect(third.messages[0].content[0]).toMatchObject({ artifact: { backendStatus: "completed" } });
  });

  it("does not use assistant-ui toolStatuses as a canonical cache input", () => {
    const convert = createTransportViewConverter();
    const canonical = message("m-1", "assistant", "stable");
    const first = convert(state([run(1, [canonical])]), {
      ...metadata(),
      toolStatuses: { "tool-1": "running" },
    });
    const second = convert(state([run(1, [canonical])]), {
      ...metadata(),
      toolStatuses: { "tool-1": "completed" },
    });

    expect(second.messages[0]).toBe(first.messages[0]);
  });

  it("keeps isRunning semantics independent from canonical message caching", () => {
    const convert = createTransportViewConverter();
    const canonical = message("m-1", "assistant", "stable");
    const first = convert(state([run(1, [canonical], "completed")]), metadata([], true));
    const second = convert(state([run(1, [canonical], "completed")]), metadata([], false));

    expect(first.isRunning).toBe(false);
    expect(second.isRunning).toBe(false);
    expect(second.messages[0]).toBe(first.messages[0]);

    const pendingCommand = {
      type: "add-message",
      message: { role: "user" as const, parts: [{ type: "text", text: "queued" }] },
    };
    expect(convert(state([]), metadata([pendingCommand])).isRunning).toBe(true);
  });

  it("matches the existing pure converter for a mixed golden fixture", () => {
    const command = {
      type: "add-message",
      sourceId: "source-1",
      parentId: null,
      message: { role: "user" as const, parts: [{ type: "text", text: "pending" }] },
    };
    const fixture = state([run(1, [toolMessage("failed")], "failed")], {
      code: "MODEL_SELECTION_REQUIRED",
      message: "请先选择模型",
    });
    const connectionMetadata = metadata([command], true);
    const expected = toTransportThreadView(fixture, connectionMetadata);
    const actual = createTransportViewConverter()(fixture, connectionMetadata);

    expect(withoutCreatedAt(actual)).toEqual(withoutCreatedAt(expected));
  });

  it("meets the append-only conversion callback upper bound for long histories", () => {
    for (const size of [200, 1000]) {
      let callbackCount = 0;
      const convert = createTransportViewConverter({
        onCanonicalMessageConverted: () => {
          callbackCount += 1;
        },
      });
      const history = Array.from({ length: size - 1 }, (_, index) => message(`history-${index}`, "assistant", `history ${index}`));
      let active = message("active", "assistant", "partial");
      convert(state([run(1, [...history, active])]), metadata());
      for (let index = 0; index < 100; index += 1) {
        active = {
          ...active,
          parts: [{ type: "text", text: `partial ${index}`, status: "running" }],
        };
        convert(state([run(1, [...history, active])]), metadata());
      }

      expect(callbackCount).toBeLessThanOrEqual(size + 100);
    }
  });

  it("does not share cached messages between converter instances", () => {
    const canonical = message("m-1", "assistant", "task one");
    const first = createTransportViewConverter()(state([run(1, [canonical])]), metadata());
    const second = createTransportViewConverter()(state([run(1, [canonical])]), metadata());

    expect(second.messages[0]).not.toBe(first.messages[0]);
  });

  it("does not invalidate canonical messages for context-only updates", () => {
    const convert = createTransportViewConverter();
    const canonical = message("m-1", "assistant", "stable");
    const first = convert(state([run(1, [canonical])]), metadata());
    const changedContext = {
      ...state([run(1, [canonical])]),
      context_usage_ratio: 0.75,
      context_usage_used: 750,
      context_window_total: 1000,
    };
    const second = convert(changedContext, metadata());

    expect(second.messages[0]).toBe(first.messages[0]);
  });
});
