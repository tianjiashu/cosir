import { describe, expect, it, vi } from "vitest";

import { AssistantAttachController } from "@/lib/assistant/assistant-attach-controller";

describe("AssistantAttachController", () => {
  it("queues a request until the transport attach function is ready", async () => {
    const attach = vi.fn(async () => {});
    const controller = new AssistantAttachController();

    controller.request(42);
    expect(attach).not.toHaveBeenCalled();

    const registerAttach = controller.setAttach;
    registerAttach(attach);
    await controller.waitForIdle();

    expect(attach).toHaveBeenCalledTimes(1);
  });

  it("coalesces duplicate requests for one run while attach is in flight", async () => {
    let release!: () => void;
    const attach = vi.fn(() => new Promise<void>((resolve) => { release = resolve; }));
    const controller = new AssistantAttachController();
    controller.setAttach(attach);

    controller.request(42);
    controller.request(42);
    await Promise.resolve();
    expect(attach).toHaveBeenCalledTimes(1);

    release();
    await controller.waitForIdle();
    expect(attach).toHaveBeenCalledTimes(1);
  });

  it("allows a failed attach to be retried", async () => {
    const attach = vi.fn()
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce(undefined);
    const onError = vi.fn();
    const controller = new AssistantAttachController(onError);
    controller.setAttach(attach);

    controller.request(42);
    await controller.waitForIdle();
    controller.request(42);
    await controller.waitForIdle();

    expect(attach).toHaveBeenCalledTimes(2);
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("can request the same run again after an effect cleanup cancellation", async () => {
    const attach = vi.fn(async () => {});
    const controller = new AssistantAttachController();
    controller.setAttach(attach);

    controller.request(42);
    controller.cancel(42);
    controller.request(42);
    await controller.waitForIdle();

    expect(attach).toHaveBeenCalledTimes(1);
  });

  it("flushes the latest run after an older attach finishes", async () => {
    let release!: () => void;
    const attach = vi.fn()
      .mockImplementationOnce(() => new Promise<void>((resolve) => { release = resolve; }))
      .mockResolvedValueOnce(undefined);
    const controller = new AssistantAttachController();
    controller.setAttach(attach);

    controller.request(42);
    await Promise.resolve();
    controller.request(43);
    release();
    await controller.waitForIdle();

    expect(attach).toHaveBeenCalledTimes(2);
  });

  it("clears the completed run when its subscription is cancelled", async () => {
    const attach = vi.fn(async () => {});
    const controller = new AssistantAttachController();
    controller.setAttach(attach);

    controller.request(42);
    await controller.waitForIdle();
    controller.cancel(42);
    controller.request(42);
    await controller.waitForIdle();

    expect(attach).toHaveBeenCalledTimes(2);
  });
});
