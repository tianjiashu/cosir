import { describe, expect, it, vi } from "vitest";

import { createAgentEditorSession, type LoadLatestAgent, type PersistAgent } from "@/lib/agent-editor/agent-editor-session";
import type { AgentConfiguration } from "@/lib/api/configuration";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { HttpError } from "@/lib/http/http-error";

const confirmation = vi.hoisted(() => ({ choice: "primary" as "primary" | "alternative" }));
vi.mock("@/lib/confirm/confirm-store", () => ({
  requestConfirmation: async (request: { action?: () => Promise<unknown>; alternative?: { action: () => Promise<unknown> } }) => {
    await (confirmation.choice === "primary" ? request.action : request.alternative?.action)?.();
    return true;
  },
}));
vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn() }));
const choose = (choice: "primary" | "alternative") => { confirmation.choice = choice; };

const draft = { agent_id: "reviewer", role: "审查", description: "审查代码", system_prompt: "请审查。" };
const savedConfiguration = (agentId: string) => ({ agent_id: agentId, revision: `rev-${agentId}` }) as AgentConfiguration;
const loadLatest = vi.fn<LoadLatestAgent>();

describe("AgentEditorSession", () => {
  it("工具草稿首存 create、再存 update，保存后身份与范围锁定", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    const persist = vi.fn<PersistAgent>(async (input) => savedConfiguration(input.agent_id));
    expect(session.store.getState().dirty).toBe(true);

    await session.save(persist, loadLatest);
    expect(persist).toHaveBeenLastCalledWith(expect.objectContaining({ agent_id: "reviewer", max_steps: 100, allowed_tool_groups: [] }), { scope: "workspace", agentId: null, baseRevision: null });
    expect(session.store.getState().dirty).toBe(false);

    session.setScope("system");
    expect(session.store.getState().scope).toBe("workspace");
    session.setField("role", "资深审查");
    await session.save(persist, loadLatest);
    expect(persist).toHaveBeenLastCalledWith(expect.objectContaining({ role: "资深审查" }), { scope: "workspace", agentId: "reviewer", baseRevision: "rev-reviewer" });
  });

  it("按基线判定未保存：改回原值即不再未保存", () => {
    const session = createAgentEditorSession({ initial: { ...draft, allowed_tool_groups: [], max_steps: 5, model_config_id: null, model_settings: {} }, saved: { revision: "r0" }, scope: "system" });
    expect(session.store.getState().dirty).toBe(false);
    session.setField("role", "别的");
    expect(session.store.getState().dirty).toBe(true);
    session.setField("role", "审查");
    expect(session.store.getState().dirty).toBe(false);
  });

  it("空白新建在修改前不算未保存", () => {
    const session = createAgentEditorSession({ initial: null, saved: false, scope: "system" });
    expect(session.store.getState().dirty).toBe(false);
    session.setField("agent_id", "new");
    expect(session.store.getState().dirty).toBe(true);
  });

  it("最大步数为空时报用户可读错误且不调用持久化", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    const persist = vi.fn<PersistAgent>();
    session.setField("max_steps", " ");
    await expect(session.save(persist, loadLatest)).rejects.toBeInstanceOf(UserFacingError);
    expect(persist).not.toHaveBeenCalled();
    expect(session.store.getState().baseline).toBeNull();
  });

  it("保存进行中不再发起第二次保存，范围也不可改", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    let resolve!: (value: AgentConfiguration) => void;
    const persist = vi.fn<PersistAgent>(() => new Promise((done) => { resolve = done; }));
    const first = session.save(persist, loadLatest);
    expect(session.store.getState().saving).toBe(true);
    await expect(session.save(persist, loadLatest)).resolves.toBe(false);
    session.setScope("system");
    expect(session.store.getState().scope).toBe("workspace");
    resolve(savedConfiguration("reviewer"));
    await expect(first).resolves.toBe(true);
    expect(persist).toHaveBeenCalledTimes(1);
    expect(session.store.getState().saving).toBe(false);
  });

  it("保存失败后解除保存中状态，仍可重试", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    await expect(session.save(async () => { throw new Error("网络错误"); }, loadLatest)).rejects.toThrow("网络错误");
    expect(session.store.getState()).toMatchObject({ saving: false, savedAgentId: null });
  });

  it("保存期间的新编辑保留并继续算作未保存", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    let resolve!: (value: AgentConfiguration) => void;
    const saving = session.save(() => new Promise((done) => { resolve = done; }), loadLatest);
    session.setField("description", "保存期间的修改");
    resolve(savedConfiguration("reviewer"));
    await saving;
    expect(session.store.getState().form.description).toBe("保存期间的修改");
    expect(session.store.getState().dirty).toBe(true);
  });

  it("Agent ID 是身份：首存进行中与保存后都改不动，之后的 update 地址与请求体一致", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    let resolve!: (value: AgentConfiguration) => void;
    const first = session.save(() => new Promise((done) => { resolve = done; }), loadLatest);
    session.setField("agent_id", "renamed");
    expect(session.store.getState().form.agent_id).toBe("reviewer");
    resolve(savedConfiguration("reviewer"));
    await first;

    session.setField("agent_id", "renamed");
    session.setField("role", "资深审查");
    const persist = vi.fn<PersistAgent>(async (input) => savedConfiguration(input.agent_id));
    await session.save(persist, loadLatest);
    const [input, target] = persist.mock.calls[0];
    expect(input.agent_id).toBe("reviewer");
    expect(target.agentId).toBe("reviewer");
  });

  it("文件被别处改过：选覆盖以当前版本为基准重存，选载入以最新配置重置表单", async () => {
    const saved = { ...draft, allowed_tool_groups: [], max_steps: 5, model_config_id: null, model_settings: {} };
    const conflict = new HttpError("配置文件已在别处修改，保存未生效", {
      status: 409, code: "configuration_revision_conflict", detail: { current_revision: "r1" },
    });

    choose("primary");
    const overwrite = createAgentEditorSession({ initial: saved, saved: { revision: "r0" }, scope: "system" });
    overwrite.setField("role", "我的版本");
    const persist = vi.fn<PersistAgent>()
      .mockRejectedValueOnce(conflict)
      .mockResolvedValueOnce({ ...savedConfiguration("reviewer"), revision: "r2" });
    await expect(overwrite.save(persist, loadLatest)).resolves.toBe(true);
    expect(persist.mock.calls.map(([, target]) => target.baseRevision)).toEqual(["r0", "r1"]);
    expect(overwrite.store.getState()).toMatchObject({ revision: "r2", dirty: false });

    choose("alternative");
    const reload = createAgentEditorSession({ initial: saved, saved: { revision: "r0" }, scope: "system" });
    reload.setField("role", "我的版本");
    const latest = { ...savedConfiguration("reviewer"), ...saved, role: "外部版本", revision: "r1" };
    const load = vi.fn<LoadLatestAgent>(async () => latest);
    await expect(reload.save(vi.fn<PersistAgent>().mockRejectedValueOnce(conflict), load)).resolves.toBe(false);
    expect(load).toHaveBeenCalledWith({ scope: "system", agentId: "reviewer" });
    expect(reload.store.getState()).toMatchObject({ revision: "r1", dirty: false, form: { role: "外部版本" } });
  });
});
