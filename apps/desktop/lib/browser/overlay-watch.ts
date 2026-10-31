/**
 * 观察有没有页面弹层盖到内置浏览器的显示区域上（设计稿 §6.1）。
 *
 * 网页是叠在整个页面之上的原生画面，CSS 层叠对它无效，弹层（菜单、对话框、Tooltip、Toast）会被它盖住。
 * 浏览器占位组件在被盖住时先放一张冻结帧、再收起原生画面。
 *
 * 判断方式不依赖组件库：项目同时用 Base UI、Radix、cmdk 与 Sonner，它们的弹层都挂在 `body` 的直接子节点
 * （portal）里，Sonner 的 Toast 在 `[data-sonner-toaster]` 里。所以「`#root` 以外的 body 子节点 + Toaster
 * 里，有没有可见元素的矩形与显示区域相交」就是判据。有弹层时按动画帧复核（弹层有入场动画、会移动），
 * 没有弹层时不做任何事。
 */

export type Rect = { left: number; top: number; right: number; bottom: number };

/** 只读用到的几个 DOM 能力，便于不带 DOM 环境的单测用替身。 */
export type OverlayElement = {
  id?: string;
  tagName: string;
  children: ArrayLike<OverlayElement>;
  getBoundingClientRect(): Rect;
};

const IGNORED_TAGS = new Set(["SCRIPT", "STYLE", "TEMPLATE", "LINK", "META"]);
/** portal 容器常是 0×0 的包装层，可见内容在里面几层；不往更深处找，代价可控。 */
const MAX_DEPTH = 3;

export function intersects(a: Rect, b: Rect): boolean {
  return a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
}

function isVisible(rect: Rect): boolean {
  return rect.right - rect.left > 0 && rect.bottom - rect.top > 0;
}

/** 某个弹层根下有没有盖到 `target` 的可见元素。 */
export function coversTarget(root: OverlayElement, target: Rect, depth = 0): boolean {
  if (IGNORED_TAGS.has(root.tagName)) return false;
  const rect = root.getBoundingClientRect();
  if (isVisible(rect)) return intersects(rect, target);
  if (depth >= MAX_DEPTH) return false;
  return Array.from(root.children).some((child) => coversTarget(child, target, depth + 1));
}

/** `body` 下除应用根以外的弹层根，加上 Sonner 的 Toast 区域。 */
export function overlayRoots(body: OverlayElement, toasters: ArrayLike<OverlayElement>): OverlayElement[] {
  const portals = Array.from(body.children).filter((child) => child.id !== "root" && !IGNORED_TAGS.has(child.tagName));
  return [...portals, ...Array.from(toasters).flatMap((toaster) => Array.from(toaster.children))];
}

/** 弹层根里有没有任何可见元素（空的常驻 portal 容器不算，免得一直按帧复核）。 */
export function hasVisibleContent(root: OverlayElement, depth = 0): boolean {
  if (IGNORED_TAGS.has(root.tagName)) return false;
  if (isVisible(root.getBoundingClientRect())) return true;
  if (depth >= MAX_DEPTH) return false;
  return Array.from(root.children).some((child) => hasVisibleContent(child, depth + 1));
}

export function isCovered(target: Rect, roots: readonly OverlayElement[]): boolean {
  return roots.some((root) => coversTarget(root, target));
}

/**
 * 观察 `target` 是否被弹层盖住，状态变化时回调；返回停止函数。
 *
 * `body` 的子节点增删由 `MutationObserver` 发现（portal 挂载 / 卸载）；常驻 portal 容器与 Toaster 里内容
 * 的变化也要发现，所以观察 body 子树，但跳过应用自身的变化。真正量矩形只在有可见弹层时按动画帧进行。
 */
export function watchOverlays(target: HTMLElement, onChange: (covered: boolean) => void): () => void {
  let covered = false;
  let frame: number | null = null;

  const roots = () => overlayRoots(document.body, document.querySelectorAll("[data-sonner-toaster]")) as unknown as OverlayElement[];

  const check = () => {
    frame = null;
    const current = roots().filter((root) => hasVisibleContent(root));
    const next = current.length > 0 && isCovered(target.getBoundingClientRect(), current);
    if (next !== covered) {
      covered = next;
      onChange(covered);
    }
    // 还有弹层根（可能在动画中移动）就下一帧再看；全部消失后停下。
    if (current.length > 0) frame = requestAnimationFrame(check);
  };
  const schedule = () => {
    if (frame === null) frame = requestAnimationFrame(check);
  };

  // 应用自身（#root）的变化很频繁，忽略；但 Toaster 渲染在 #root 里，它的变化要算。
  const relevant = (node: Node) => {
    const element = node instanceof Element ? node : node.parentElement;
    const appRoot = document.getElementById("root");
    return !appRoot?.contains(node) || Boolean(element?.closest("[data-sonner-toaster]"));
  };
  const observer = new MutationObserver((mutations) => {
    if (mutations.some((mutation) => relevant(mutation.target))) schedule();
  });
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["style", "class", "data-open", "data-state", "hidden"] });
  schedule();

  return () => {
    observer.disconnect();
    if (frame !== null) cancelAnimationFrame(frame);
  };
}
