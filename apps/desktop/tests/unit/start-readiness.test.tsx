import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { FirstRunChecklist } from "@/components/onboarding/first-run-checklist";
import { modelSetupState, shouldShowFirstRunChecklist, startBlockedReason } from "@/components/onboarding/start-readiness";
import { buildModelCatalog } from "@/lib/model-catalog";

describe("modelSetupState", () => {
  it("maps catalog snapshots to setup states", () => {
    expect(modelSetupState({ status: "idle", catalog: null })).toBe("loading");
    expect(modelSetupState({ status: "loading", catalog: null })).toBe("loading");
    expect(modelSetupState({ status: "error", catalog: null })).toBe("error");
    expect(modelSetupState({ status: "ready", catalog: buildModelCatalog([]) })).toBe("empty");
    expect(modelSetupState({
      status: "ready",
      catalog: buildModelCatalog([{
        config_id: 1,
        config_name: "Demo",
        model_name: "demo-model",
        context_window_k: 128,
        supports_reasoning_effort: false,
        supports_image: false,
      } as Parameters<typeof buildModelCatalog>[0][number]]),
    })).toBe("ready");
  });
});

describe("shouldShowFirstRunChecklist", () => {
  it("does not flash the workspace step while the workspace catalog is still loading", () => {
    expect(shouldShowFirstRunChecklist({ workspacesLoading: true, hasWorkspaces: false, modelState: "ready" })).toBe(false);
    expect(shouldShowFirstRunChecklist({ workspacesLoading: false, hasWorkspaces: false, modelState: "ready" })).toBe(true);
  });

  it("shows for a missing model and hides once both prerequisites are met", () => {
    expect(shouldShowFirstRunChecklist({ workspacesLoading: false, hasWorkspaces: true, modelState: "empty" })).toBe(true);
    expect(shouldShowFirstRunChecklist({ workspacesLoading: false, hasWorkspaces: true, modelState: "error" })).toBe(true);
    expect(shouldShowFirstRunChecklist({ workspacesLoading: false, hasWorkspaces: true, modelState: "loading" })).toBe(false);
    expect(shouldShowFirstRunChecklist({ workspacesLoading: false, hasWorkspaces: true, modelState: "ready" })).toBe(false);
  });
});

describe("startBlockedReason", () => {
  const base = { hasWorkspaces: true, selectedWorkspaceId: 7, modelState: "ready" as const, modelReady: true };

  it("explains missing workspace before missing model", () => {
    expect(startBlockedReason({ ...base, hasWorkspaces: false, selectedWorkspaceId: null, modelState: "empty", modelReady: false })).toBe("先添加工作区");
    expect(startBlockedReason({ ...base, selectedWorkspaceId: null })).toBe("先选择工作区");
    expect(startBlockedReason({ ...base, modelState: "empty", modelReady: false })).toBe("先配置模型");
    expect(startBlockedReason({ ...base, modelState: "error", modelReady: false })).toBe("先配置模型");
  });

  it("stays quiet while the model catalog loads or when everything is ready", () => {
    expect(startBlockedReason({ ...base, modelState: "loading", modelReady: false })).toBeNull();
    expect(startBlockedReason(base)).toBeNull();
  });
});

describe("FirstRunChecklist", () => {
  const render = (props: Partial<Parameters<typeof FirstRunChecklist>[0]>) => renderToStaticMarkup(
    <MemoryRouter>
      <FirstRunChecklist
        hasWorkspace={false}
        modelState="empty"
        onAddWorkspace={() => undefined}
        addingWorkspace={false}
        addWorkspaceError={null}
        {...props}
      />
    </MemoryRouter>,
  );

  it("offers both actions when nothing is set up", () => {
    const html = render({});
    expect(html).toMatch(/添加工作区<span[^>]*>（未完成）/);
    expect(html).toMatch(/配置模型<span[^>]*>（未完成）/);
    expect(html).toContain('href="/settings/models"');
  });

  it("marks completed steps and drops their actions", () => {
    const html = render({ hasWorkspace: true });
    expect(html).toMatch(/添加工作区<span[^>]*>（已完成）/);
    expect(html).not.toContain("选择一个项目文件夹");
  });

  it("does not ask the user to configure a model while the catalog is loading", () => {
    const html = render({ hasWorkspace: true, modelState: "loading" });
    expect(html).toContain("正在检查可用模型");
    expect(html).not.toContain('href="/settings/models"');
  });
});
