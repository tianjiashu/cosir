import { describe, expect, it } from "vitest";

import { resolveTransportFailurePolicy } from "@/components/assistant/runtime/transport-failure-policy";

describe("Transport 失败处置策略", () => {
  it("attach 返回非 2xx 时不弹发送失败，读取快照并回到有界 recovery", () => {
    expect(resolveTransportFailurePolicy({ request: "attach", phase: "request-rejected" })).toEqual({
      presentation: "status",
      restoreComposer: false,
      snapshot: "import-then-recover",
    });
  });

  it("attach 已建立后流中断，同样回到快照对账与 recovery", () => {
    expect(resolveTransportFailurePolicy({ request: "attach", phase: "accepted-stream-failed" })).toEqual({
      presentation: "status",
      restoreComposer: false,
      snapshot: "import-then-recover",
    });
  });

  it("提交命令被拒时弹窗并恢复草稿，不读取快照", () => {
    expect(resolveTransportFailurePolicy({ request: "commands", phase: "request-rejected" })).toEqual({
      presentation: "dialog",
      restoreComposer: true,
      snapshot: "none",
    });
  });

  it("命令已被接受但流中断时只导入快照，不恢复草稿也不弹窗", () => {
    expect(resolveTransportFailurePolicy({ request: "commands", phase: "accepted-stream-failed" })).toEqual({
      presentation: "status",
      restoreComposer: false,
      snapshot: "import",
    });
  });
});
