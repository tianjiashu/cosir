/**
 * 输入框外框：只装「写什么」——正文、「+」与发送。「怎么跑」（模型、终端权限、上下文）在框外的配置行
 * （`composer-config-bar.tsx`）。收窄判断的容器（`@container`）在二者共同的外层，见 `thread.aui.tsx`。
 */
export const COMPOSER_SURFACE_CLASS_NAME =
  "border-border/60 bg-card focus-within:border-brand/60 focus-within:ring-brand/20 focus-within:ring-2 flex min-w-0 w-full flex-col gap-2 rounded-3xl border p-2 shadow-sm";

/**
 * 输入框里图标按钮（「+」添加菜单）的统一外观：只放图标，名称走 `title` 与 `aria-label`；内容入口收进
 * 「+」菜单，在菜单里才有名称与说明（`ComposerMenuRow`）。
 */
export const COMPOSER_ICON_BUTTON_CLASS_NAME =
  "text-muted-foreground hover:text-foreground hover:bg-muted-foreground/15 relative inline-flex size-7 shrink-0 items-center justify-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent [&_svg]:size-4";

/**
 * 框外配置行里的按钮（模型、终端权限、上下文）的统一外观：没有图标与箭头的淡色短词，悬停与菜单打开时
 * 底色明显加深（`aria-expanded`），白底上也看得出能点。配合 `variant="ghost"` 或原生按钮使用。
 * 默认不收缩、不折行：窄宽度下只有名称可能很长的模型按钮放开收缩（`min-w-0 shrink`）并截断。
 */
export const COMPOSER_CONFIG_BUTTON_CLASS_NAME =
  "text-muted-foreground hover:text-foreground hover:bg-muted-foreground/12 aria-expanded:bg-muted-foreground/12 aria-expanded:text-foreground dark:hover:bg-muted-foreground/20 dark:aria-expanded:bg-muted-foreground/20 inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md px-2 text-xs font-normal transition-colors outline-none focus-visible:ring-2 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50";
