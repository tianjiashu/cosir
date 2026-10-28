import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { CancelTaskResult } from "@/lib/assistant/task-commands";

const captured = vi.hoisted(() => ({
  props: null as Record<string, unknown> | null,
  cancelRun: vi.fn(async (): Promise<CancelTaskResult> => ({ accepted: true })),
}));

vi.mock("@/components/ui/button", () => ({
  Button: (props: Record<string, unknown>) => {
    captured.props = props;
    return null;
  },
}));
vi.mock("@/lib/assistant/task-commands", () => ({
  cancelTask: captured.cancelRun,
}));

describe("StopButton", () => {
  it("sends backend cancellation without aborting the Assistant Transport subscription", async () => {
    const { StopButton } = await import("@/components/assistant/stop-button");
    const assistantCancel = vi.fn();
    const onCancelRequested = vi.fn();
    const onCancelResult = vi.fn();
    const event = {} as React.MouseEvent<HTMLButtonElement>;

    renderToStaticMarkup(
      <StopButton
        taskId={42}
        onClick={assistantCancel}
        onCancelRequested={onCancelRequested}
        onCancelResult={onCancelResult}
      />,
    );
    const onClick = captured.props?.onClick;
    expect(onClick).toEqual(expect.any(Function));

    (onClick as (event: React.MouseEvent<HTMLButtonElement>) => void)(event);
    await vi.waitFor(() => expect(captured.cancelRun).toHaveBeenCalledWith(42));

    expect(assistantCancel).not.toHaveBeenCalled();
    expect(onCancelRequested).toHaveBeenCalledWith();
    expect(onCancelResult).toHaveBeenCalledWith(true);
    expect(onCancelRequested.mock.invocationCallOrder[0]).toBeLessThan(captured.cancelRun.mock.invocationCallOrder.at(-1)!);
    expect(captured.cancelRun.mock.invocationCallOrder.at(-1)!).toBeLessThan(onCancelResult.mock.invocationCallOrder[0]);
  });

  it("does not invoke assistant-ui Cancel when backend cancellation is rejected", async () => {
    const { StopButton } = await import("@/components/assistant/stop-button");
    const assistantCancel = vi.fn();
    const onCancelRequested = vi.fn();
    const onCancelResult = vi.fn();
    captured.cancelRun.mockResolvedValueOnce({
      accepted: false,
      reason: "rejected",
      message: "取消失败",
    });

    renderToStaticMarkup(
      <StopButton
        taskId={42}
        onClick={assistantCancel}
        onCancelRequested={onCancelRequested}
        onCancelResult={onCancelResult}
      />,
    );
    const onClick = captured.props?.onClick;
    (onClick as (event: React.MouseEvent<HTMLButtonElement>) => void)({} as React.MouseEvent<HTMLButtonElement>);
    await vi.waitFor(() => expect(captured.cancelRun).toHaveBeenCalledWith(42));

    expect(assistantCancel).not.toHaveBeenCalled();
    expect(onCancelRequested).toHaveBeenCalledWith();
    expect(onCancelResult).toHaveBeenCalledWith(false);
  });

  it("forwards composition props and ref to the styled button", async () => {
    const { StopButton } = await import("@/components/assistant/stop-button");
    const assistantCancel = vi.fn();
    const ref = { current: null };

    renderToStaticMarkup(
      <StopButton
        taskId={42}
        ref={ref}
        onClick={assistantCancel}
        data-testid="stop-button"
        aria-label="custom-stop"
        disabled
      />,
    );

    expect(captured.props?.["data-testid"]).toBe("stop-button");
    expect(captured.props?.["aria-label"]).toBe("custom-stop");
    expect(captured.props?.disabled).toBe(true);
    expect(captured.props?.ref).toBe(ref);
  });

  it("keeps the button disabled while the workflow settles cancellation", async () => {
    const { StopButton } = await import("@/components/assistant/stop-button");

    renderToStaticMarkup(
      <StopButton taskId={42} isCancelling />,
    );

    expect(captured.props?.disabled).toBe(true);
    expect(captured.props?.["aria-label"]).toBe("正在停止");
  });
});
