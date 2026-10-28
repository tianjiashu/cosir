import { describe, expect, it, vi } from "vitest";

import { createAssistantPerformanceProbe } from "@/lib/assistant/assistant-performance-probe";

describe("AssistantPerformanceProbe", () => {
  it("aggregates transport and React commit measurements into one stream report", async () => {
    const onReport = vi.fn();
    const probe = createAssistantPerformanceProbe({ onReport });

    probe.beginStream();
    probe.recordFrame({ kind: "mutation", mutationCount: 3, durationMs: 2 });
    probe.recordFrame({ kind: "mutation", mutationCount: 1, durationMs: 5 });
    probe.recordPublication({ durationMs: 7, itemCount: 1000, activeMessageId: "active", publicationKind: "stream", animationFrameTimestamp: 1, terminalConvergence: false });
    probe.recordReactCommit({ scope: "message-row", id: "active", phase: "update", actualDurationMs: 4 });
    probe.recordReactCommit({ scope: "message-row", id: "history", phase: "update", actualDurationMs: 9 });
    probe.recordReactCommit({ scope: "message-row", id: "active", phase: "update", actualDurationMs: 1 });
    probe.recordReactCommit({ scope: "composer", id: "composer", phase: "update", actualDurationMs: 1 });
    probe.finishStream({ activityRunId: 1, activityStatus: "completed" });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(onReport).toHaveBeenCalledTimes(1);
    expect(onReport).toHaveBeenCalledWith(expect.objectContaining({
      activityRunId: 1,
      activityStatus: "completed",
      frameCount: 2,
      mutationCount: 4,
      publicationCount: 1,
      maxApplyFrameMs: 5,
      maxPublicationMs: 7,
      activeRowCommitCount: 2,
      historicalRowCommitCount: 1,
      maxActiveRowCommitsPerFrame: 2,
      canonicalConvergenceCount: 0,
      pendingProjectionPublicationCount: 0,
      composerCommitCount: 1,
      terminalConvergenceCount: 0,
    }));
  });

  it("ignores React commits outside an active stream", async () => {
    const onReport = vi.fn();
    const probe = createAssistantPerformanceProbe({ onReport });

    probe.recordReactCommit({ scope: "message-row", id: "history", phase: "mount", actualDurationMs: 2 });
    probe.beginStream();
    probe.finishStream({ activityRunId: 1, activityStatus: "running" });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(onReport.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      activeRowCommitCount: 0,
      historicalRowCommitCount: 0,
    }));
  });

  it("waits one task before reporting so the final React commit is included", async () => {
    const onReport = vi.fn();
    const probe = createAssistantPerformanceProbe({ onReport });

    probe.beginStream();
    probe.finishStream({ activityRunId: 1, activityStatus: "completed" });
    probe.recordReactCommit({ scope: "message-row", id: "active", phase: "update", actualDurationMs: 4 });

    expect(onReport).not.toHaveBeenCalled();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(onReport.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      activeRowCommitCount: 0,
      historicalRowCommitCount: 1,
    }));
  });

  it("counts terminal convergence once and limits active row commits per publication", async () => {
    const onReport = vi.fn();
    const probe = createAssistantPerformanceProbe({ onReport });

    probe.beginStream();
    probe.recordPublication({ durationMs: 1, itemCount: 2, activeMessageId: "active", publicationKind: "stream", animationFrameTimestamp: 1, terminalConvergence: false });
    probe.recordReactCommit({ scope: "message-row", id: "active", phase: "update", actualDurationMs: 1 });
    probe.recordReactCommit({ scope: "message-row", id: "active", phase: "update", actualDurationMs: 1 });
    probe.recordTerminalConvergence();
    probe.recordPublication({ durationMs: 1, itemCount: 2, activeMessageId: "active", publicationKind: "canonical", terminalConvergence: true });
    probe.finishStream({ activityRunId: 1, activityStatus: "completed" });
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(onReport.mock.calls[0]?.[0]).toEqual(expect.objectContaining({
      activeRowCommitCount: 2,
      maxActiveRowCommitsPerFrame: 2,
      terminalConvergenceCount: 1,
    }));
  });
});
