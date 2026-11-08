import { beforeEach, describe, expect, it } from "vitest";

import {
  callKey,
  processKey,
  activityKey,
  resolveDisclosure,
  useDisclosureStore,
} from "@/lib/assistant/turn/disclosure-store";

describe("disclosure store", () => {
  beforeEach(() => useDisclosureStore.setState({ tasks: {} }));

  it("没有覆盖时按调用方给的默认值；默认值随阶段变化，不被旧状态卡住", () => {
    expect(resolveDisclosure(1, processKey("m1"), true)).toBe(true);
    expect(resolveDisclosure(1, processKey("m1"), false)).toBe(false);
  });

  it("只记用户覆盖，覆盖优先于默认值；reset 后回到默认规则", () => {
    const { set, reset } = useDisclosureStore.getState();
    set(1, processKey("m1"), false);
    expect(resolveDisclosure(1, processKey("m1"), true)).toBe(false);
    set(1, callKey("c1"), true);
    set(1, activityKey("m1", "3"), true);
    expect(resolveDisclosure(1, callKey("c1"), false)).toBe(true);
    expect(resolveDisclosure(1, activityKey("m1", "3"), false)).toBe(true);
    // 另一个 part 下标的思考行不受影响。
    expect(resolveDisclosure(1, activityKey("m1", "4"), false)).toBe(false);

    reset(1, processKey("m1"));
    expect(resolveDisclosure(1, processKey("m1"), true)).toBe(true);
    expect(useDisclosureStore.getState().tasks[1]).toEqual({ [callKey("c1")]: true, [activityKey("m1", "3")]: true });
  });

  it("按 Task 分桶：同名键在不同 Task 互不影响", () => {
    useDisclosureStore.getState().set(1, callKey("c1"), true);
    expect(resolveDisclosure(2, callKey("c1"), false)).toBe(false);
  });

  it("clearTask 丢掉整桶，不影响其他 Task", () => {
    const { set, clearTask } = useDisclosureStore.getState();
    set(1, callKey("c1"), true);
    set(2, callKey("c1"), true);
    clearTask(1);
    expect(useDisclosureStore.getState().tasks).toEqual({ 2: { [callKey("c1")]: true } });
    // 不存在的 Task 不产生新状态对象。
    const before = useDisclosureStore.getState();
    clearTask(9);
    expect(useDisclosureStore.getState()).toBe(before);
  });
});
