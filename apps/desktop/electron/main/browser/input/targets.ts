import type { PageAction, PointerTarget } from "../params";

/** 动作指向哪里（元素或坐标）；不指向页面上某处的动作（等待、回答弹窗、不带 ref 的按键与滚动）为 null。 */
export function targetOf(action: PageAction): PointerTarget | null {
  switch (action.type) {
    case "click":
    case "hover":
      return action.target;
    case "drag":
      return action.start;
    case "type":
    case "select":
      return { ref: action.ref };
    case "press":
    case "scroll":
    case "upload":
      return action.ref ? { ref: action.ref } : null;
    case "wait_for":
    case "dialog":
      return null;
  }
}

/**
 * 给人看的目标说明（工具卡片的每一步）：元素写快照里的角色与名称（`label`，取不到就写 ref），坐标写 `(x, y)`，
 * 拖动写起点到终点。说明来自网页文字，只用于显示。
 */
export function describeTarget(action: PageAction, label: (ref: string) => string | undefined): string | null {
  const describe = (target: PointerTarget) => ("ref" in target ? label(target.ref) ?? target.ref : `(${Math.round(target.x)}, ${Math.round(target.y)})`);
  if (action.type === "drag") return `${describe(action.start)} → ${describe(action.end)}`;
  const target = targetOf(action);
  return target ? describe(target) : null;
}
