import { describe, expect, it, vi } from "vitest";

import { createAgentEditorSession, type PersistAgent } from "@/lib/agent-editor/session";
import type { AgentConfiguration } from "@/lib/api/configuration";
import { UserFacingError } from "@/lib/errors/user-facing-error";

const draft = { agent_id: "reviewer", role: "审查", description: "审查代码", system_prompt: "请审查。" };
const savedConfiguration = (agentId: string) => ({ agent_id: agentId }) as AgentConfiguration;

describe("AgentEditorSession", () => {
  it("工具草稿首存 create、再存 update，保存后身份与范围锁定", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    const persist = vi.fn<PersistAgent>(async (input) => savedConfiguration(input.agent_id));
    expect(session.store.getState().dirty).toBe(true);

    await session.save(persist);
    expect(persist).toHaveBeenLastCalledWith(expect.objectContaining({ agent_id: "reviewer", max_steps: 100, allowed_tool_groups: [] }), { scope: "workspace", agentId: null });
    expect(session.store.getState().dirty).toBe(false);

    session.setScope("system");
    expect(session.store.getState().scope).toBe("workspace");
    session.setField("role", "资深审查");
    await session.save(persist);
    expect(persist).toHaveBeenLastCalledWith(expect.objectContaining({ role: "资深审查" }), { scope: "workspace", agentId: "reviewer" });
  });

  it("按基线判定未保存：改回原值即不再未保存", () => {
    const session = createAgentEditorSession({ initial: { ...draft, allowed_tool_groups: [], max_steps: 5, model_config_id: null, model_settings: {} }, saved: true, scope: "system" });
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
    await expect(session.save(persist)).rejects.toBeInstanceOf(UserFacingError);
    expect(persist).not.toHaveBeenCalled();
    expect(session.store.getState().baseline).toBeNull();
  });

  it("保存进行中不再发起第二次保存，范围也不可改", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    let resolve!: (value: AgentConfiguration) => void;
    const persist = vi.fn<PersistAgent>(() => new Promise((done) => { resolve = done; }));
    const first = session.save(persist);
    expect(session.store.getState().saving).toBe(true);
    await expect(session.save(persist)).resolves.toBe(false);
    session.setScope("system");
    expect(session.store.getState().scope).toBe("workspace");
    resolve(savedConfiguration("reviewer"));
    await expect(first).resolves.toBe(true);
    expect(persist).toHaveBeenCalledTimes(1);
    expect(session.store.getState().saving).toBe(false);
  });

  it("保存失败后解除保存中状态，仍可重试", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    await expect(session.save(async () => { throw new Error("网络错误"); })).rejects.toThrow("网络错误");
    expect(session.store.getState()).toMatchObject({ saving: false, savedAgentId: null });
  });

  it("保存期间的新编辑保留并继续算作未保存", async () => {
    const session = createAgentEditorSession({ initial: draft, saved: false, scope: "workspace" });
    let resolve!: (value: AgentConfiguration) => void;
    const saving = session.save(() => new Promise((done) => { resolve = done; }));
    session.setField("description", "保存期间的修改");
    resolve(savedConfiguration("reviewer"));
    await saving;
    expect(session.store.getState().form.description).toBe("保存期间的修改");
    expect(session.store.getState().dirty).toBe(true);
  });
});
