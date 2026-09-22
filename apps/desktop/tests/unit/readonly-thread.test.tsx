import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const captured = vi.hoisted(() => ({
  provider: null as Record<string, unknown> | null,
  thread: null as Record<string, unknown> | null,
}));

vi.mock("@assistant-ui/react", () => ({
  ReadonlyThreadProvider: ({ children, ...props }: { children: unknown }) => {
    captured.provider = props;
    return children;
  },
}));

vi.mock("@/components/assistant-ui/elements/thread.aui", () => ({
  Thread: (props: Record<string, unknown>) => {
    captured.thread = props;
    return null;
  },
}));

import { ReadonlyThread } from "@/components/assistant-ui/elements/readonly-thread.aui";

describe("ReadonlyThread", () => {
  beforeEach(() => {
    captured.provider = null;
    captured.thread = null;
  });

  it("mounts the Workbench child surface as read-only without a composer", () => {
    renderToStaticMarkup(<ReadonlyThread messages={[]} taskId={501} />);

    expect(captured.provider?.messages).toEqual([]);
    expect(captured.thread).toMatchObject({ readonly: true, autoFocus: false, taskId: 501 });
  });
});
