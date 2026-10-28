import { describe, expect, it } from "vitest";

import { reconcileTerminalOutput } from "@/components/assistant-ui/tools/terminal-output-reconciler";

describe("reconcileTerminalOutput", () => {
  it("appends only the new suffix of a streaming snapshot", () => {
    expect(reconcileTerminalOutput("install 1", "install 1\rinstall 2")).toEqual({ kind: "append", text: "\rinstall 2" });
  });

  it("does not replay identical snapshots", () => {
    expect(reconcileTerminalOutput("same", "same")).toEqual({ kind: "ignore" });
  });

  it("requests a reset when a terminal snapshot is no longer an append-only prefix", () => {
    expect(reconcileTerminalOutput("partial output", "final screen\u001b[2K")).toEqual({ kind: "reset", text: "final screen\u001b[2K" });
  });
});
