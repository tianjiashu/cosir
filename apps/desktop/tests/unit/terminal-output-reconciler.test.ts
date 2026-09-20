import { describe, expect, it } from "vitest";

import { reconcileTerminalOutput } from "@/components/assistant-ui/tools/terminal-output-reconciler";

describe("reconcileTerminalOutput", () => {
  it("appends only the new suffix of a streaming snapshot", () => {
    expect(reconcileTerminalOutput({
      previousOutput: "install 1",
      previousSeq: 1,
      nextOutput: "install 1\rinstall 2",
      nextSeq: 2,
    })).toEqual({ kind: "append", text: "\rinstall 2" });
  });

  it("does not replay identical or stale snapshots", () => {
    expect(reconcileTerminalOutput({
      previousOutput: "same",
      previousSeq: 3,
      nextOutput: "same",
      nextSeq: 3,
    })).toEqual({ kind: "ignore" });
    expect(reconcileTerminalOutput({
      previousOutput: "latest",
      previousSeq: 3,
      nextOutput: "older",
      nextSeq: 2,
    })).toEqual({ kind: "ignore" });
  });

  it("requests a reset when a terminal snapshot is no longer an append-only prefix", () => {
    expect(reconcileTerminalOutput({
      previousOutput: "partial output",
      previousSeq: 4,
      nextOutput: "final screen\u001b[2K",
      nextSeq: 5,
    })).toEqual({ kind: "reset", text: "final screen\u001b[2K" });
  });

  it("resynchronizes when an equal sequence unexpectedly carries different output", () => {
    expect(reconcileTerminalOutput({
      previousOutput: "old screen",
      previousSeq: 4,
      nextOutput: "new screen",
      nextSeq: 4,
    })).toEqual({ kind: "reset", text: "new screen" });
  });
});
