/** 回合内折叠动画时长：展开 / 收起期间锁定视口滚动（`useScrollLock`）也用这个值。 */
export const DISCLOSURE_ANIMATION_MS = 200;

/** `CollapsibleContent` 的开合动画类（Base UI 的 `data-open` / `data-closed`）；减少动态效果时全部关闭。 */
export const DISCLOSURE_CONTENT_CLASS = [
  "relative overflow-hidden outline-none ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:animate-none",
  "data-closed:animate-collapsible-up data-open:animate-collapsible-down data-closed:fill-mode-forwards data-closed:pointer-events-none",
  "[--tw-duration:var(--animation-duration)]",
].join(" ");

/** 产物卡（本轮改动、草稿）共用的底色面：`--surface`、圆角、不画边框与阴影（设计 §5.8）。 */
export const SURFACE_CARD_CLASS = "bg-surface rounded-(--radius)";
