import type { PageAction } from "../params";
import { box, element, type Box } from "./element";
import type { PageSession } from "./session";
import { targetOf } from "./targets";

/** 坐标落点画成以它为中心的小方块（CSS 像素）。 */
const POINT_SIZE = 24;

/**
 * 动作执行前给人看它要操作哪里（`TabHighlight`，标签不在面板上时不画、也不量位置）。量位置会把元素滚入视野，动作
 * 本来也要滚。量不到（元素已不在、没有布局）就不画：动作自己会报同样的问题，高亮不替它报错。
 */
export async function highlightTarget(session: PageSession, action: PageAction): Promise<void> {
  const target = targetOf(action);
  if (!target) return;
  await session.highlight(() => ("ref" in target ? element(session, target.ref).then((node) => box(session, node)) : Promise.resolve(around(target))));
}

function around({ x, y }: { x: number; y: number }): Box {
  return { x: x - POINT_SIZE / 2, y: y - POINT_SIZE / 2, width: POINT_SIZE, height: POINT_SIZE };
}
