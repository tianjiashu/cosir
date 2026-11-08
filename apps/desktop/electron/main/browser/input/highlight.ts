import type { PageAction } from "../params";
import { box, type Box } from "./element";
import type { BrowserOperation } from "../operation";
import { targetOf } from "./targets";

/** 坐标落点画成以它为中心的小方块（CSS 像素）。 */
const POINT_SIZE = 24;

/**
 * 动作执行前给人看它要操作哪里（`TabHighlight`，标签不在面板上时不画、也不量位置）。量位置会把元素滚入视野，动作
 * 本来也要滚。元素还在不在页面上是动作的事（`element` 确认），这里不再确认一遍：ref 只按登记取节点，量不到（没登记、
 * 已不在、没有布局）就不画，动作自己会报同样的问题。
 */
export async function highlightTarget(session: BrowserOperation, action: PageAction): Promise<void> {
  const target = targetOf(action);
  if (!target) return;
  await session.highlight(async () => ("ref" in target ? box(session, session.resolveRef(target.ref)) : around(target)));
}

function around({ x, y }: { x: number; y: number }): Box {
  return { x: x - POINT_SIZE / 2, y: y - POINT_SIZE / 2, width: POINT_SIZE, height: POINT_SIZE };
}
