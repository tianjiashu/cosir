import { browserError } from "../errors";
import type { Modifier, PointerTarget } from "../params";
import { center, element } from "./element";
import { modifierBits } from "./modifiers";
import { effect, type PageSession } from "./session";

/**
 * 鼠标：只经 CDP 合成事件。落点按页面的 CSS 像素算，鼠标事件发出前经 `toInput` 换成视图坐标（面板缩放显示时两者
 * 不同）；拖放事件（`Input.dispatchDragEvent`）直接用 CSS 像素（实测：按视图坐标发，缩放 0.5 时落到一半处）。
 */

/** 拖动途中的移动次数：只发起点与终点时，按移动距离判定拖动开始、或跟踪位置的页面（拖放排序、滑块）收不到中间位置。 */
const DRAG_STEPS = 5;

export async function click(
  session: PageSession,
  target: PointerTarget,
  options: { button: "left" | "right"; clickCount: 1 | 2; modifiers: readonly Modifier[] },
): Promise<void> {
  const { x, y } = session.toInput(await point(session, target));
  const modifiers = modifierBits(options.modifiers);
  await session.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, modifiers });
  // 双击是两次完整的按下与松开、第二次的 clickCount 为 2，与真实输入一致：页面的 click 与 dblclick 都照常触发。
  for (let clickCount = 1; clickCount <= options.clickCount; clickCount++) {
    const event = { x, y, button: options.button, clickCount, modifiers };
    await effect(session.cdp("Input.dispatchMouseEvent", { type: "mousePressed", ...event }));
    await effect(session.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", ...event }));
  }
}

/** 悬停状态留到下一次鼠标移动；之后的 click 会移走指针，靠延迟收起的菜单一般能容忍。 */
export async function hover(session: PageSession, target: PointerTarget): Promise<void> {
  const { x, y } = session.toInput(await point(session, target));
  await effect(session.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y }));
}

/**
 * 按住左键从 `start` 移到 `end` 再松开。两端在按下前都定位好：ref 那端定位时会滚入视野、可能把先定位的一端滚走，
 * 离得远时由 Agent 先滚好再用坐标（参数说明里写明）。
 *
 * HTML5 拖放（`draggable`）要靠系统拖动会话，而不在面板上的标签视图不在任何窗口里，会话起不来、drop 永远不来
 * （实测）。所以拖动期间开 CDP 拖放拦截：页面开始拖放时浏览器不起系统会话，改发 `Input.dragIntercepted` 带上拖放
 * 数据，这里在终点依次投递 dragenter / dragover / drop——挂在面板上时也走同一条路。滑块、画布这类只听鼠标事件的
 * 拖动不会开始拖放，照常由鼠标事件完成。拦截只在本次拖动期间开着（开着时用户自己的拖放也会被拦下），收尾时
 * 不论成败都撤销；撤销由浏览器进程作答，页面被 drop 弹出的弹窗停住时也能完成。
 */
export async function drag(session: PageSession, start: PointerTarget, end: PointerTarget): Promise<void> {
  const startPoint = await point(session, start);
  const endPoint = await point(session, end);
  const from = session.toInput(startPoint);
  const to = session.toInput(endPoint);
  let dragData: unknown = null;
  const unsubscribe = session.on("Input.dragIntercepted", ({ data }: { data: unknown }) => (dragData = data));
  await session.cdp("Input.setInterceptDrags", { enabled: true }, { browserSide: true });
  try {
    await session.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", ...from });
    await effect(session.cdp("Input.dispatchMouseEvent", { type: "mousePressed", ...from, button: "left", clickCount: 1 }));
    for (let step = 1; step <= DRAG_STEPS; step++) {
      const x = from.x + ((to.x - from.x) * step) / DRAG_STEPS;
      const y = from.y + ((to.y - from.y) * step) / DRAG_STEPS;
      await effect(session.cdp("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "left", buttons: 1 }));
    }
    if (dragData) {
      for (const type of ["dragEnter", "dragOver", "drop"]) {
        await effect(session.cdp("Input.dispatchDragEvent", { type, ...endPoint, data: dragData }));
      }
    }
    await effect(session.cdp("Input.dispatchMouseEvent", { type: "mouseReleased", ...to, button: "left", clickCount: 1 }));
  } finally {
    unsubscribe();
    await session.cdp("Input.setInterceptDrags", { enabled: false }, { browserSide: true });
  }
}

/** 落点（主视口的 CSS 像素，与视口截图的像素一致）：元素取中心（先滚入视野），坐标须在视口之内。 */
async function point(session: PageSession, target: PointerTarget): Promise<{ x: number; y: number }> {
  if ("ref" in target) return center(session, await element(session, target.ref));
  const { width, height } = session.viewport();
  if (target.x >= width || target.y >= height) {
    throw browserError("OUT_OF_VIEWPORT", `(${target.x}, ${target.y}) is outside the ${width}×${height} viewport`);
  }
  return target;
}
