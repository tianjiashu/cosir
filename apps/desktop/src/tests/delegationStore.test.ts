// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from "vitest";
import { useDelegationStore } from "@/stores/delegationStore";

describe("delegationStore 选中态", () => {
  beforeEach(() => {
    // 每个用例前复位，避免用例间选中态串扰。
    useDelegationStore.getState().clearSelection();
  });

  it("初始未选中", () => {
    expect(useDelegationStore.getState().selectedChildTurnId).toBeNull();
  });

  it("selectChildTurn 选中指定 child turn", () => {
    useDelegationStore.getState().selectChildTurn("turn_child_1");

    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_1");
  });

  it("selectChildTurn 可切换选中目标", () => {
    useDelegationStore.getState().selectChildTurn("turn_child_1");
    useDelegationStore.getState().selectChildTurn("turn_child_2");

    expect(useDelegationStore.getState().selectedChildTurnId).toBe("turn_child_2");
  });

  it("clearSelection 清空选中态", () => {
    useDelegationStore.getState().selectChildTurn("turn_child_1");
    useDelegationStore.getState().clearSelection();

    expect(useDelegationStore.getState().selectedChildTurnId).toBeNull();
  });
});
