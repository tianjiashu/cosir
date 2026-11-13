/**
 * 「悬停才出现」的次要操作：只占透明度不占版面，显隐不推动文字，也不改变虚拟列表的行高。
 * 悬停所在分组、或分组内有键盘焦点（focus-visible，鼠标点过不算）时出现；触屏设备没有悬停，一直显示。
 *
 * 每个常量绑定一个**跨文件共用**的 Tailwind 分组作用域，名字与代码里的 `group/名字` 一一对应。不合成一个：
 * `group-hover/名字` 对任一同名祖先生效，消息里嵌着调用行，两层同名时悬停消息会让行里的隐藏入口全部冒出来；
 * 类名也必须是写死的字符串，Tailwind 扫描不到按作用域名拼出来的类。只在一个文件里用的分组（如
 * `group/model`、`group/file-mention`）就地写，不进这里。
 */

/** 可开合的行与卡片头（`group/disclosure`）：调用行、侧栏卡片标题行、终端卡片。 */
export const REVEAL_ON_DISCLOSURE_HOVER = "opacity-0 group-hover/disclosure:opacity-100 group-has-[:focus-visible]/disclosure:opacity-100 [@media(hover:none)]:opacity-100";

/** 一条消息（`group/message`）：用户消息与回复的操作条。 */
export const REVEAL_ON_MESSAGE_HOVER = "opacity-0 group-hover/message:opacity-100 group-has-[:focus-visible]/message:opacity-100 [@media(hover:none)]:opacity-100";
