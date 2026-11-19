/**
 * 阅读位置：视口顶部是文本的第几行（1 起，相对这段文本），以及视口顶部比这一行的开头低多少像素（可略小于 0：
 * 源码视图以首字符的字形顶部为参照，视口顶部可能落在行距里）。
 *
 * 不记像素 scrollTop：像素只在记录它的那个布局里有意义。查看器重建后 CodeMirror 先按估算行高排版、测量后
 * 才是真实行高，Markdown 预览的图片与 Mermaid 图异步加载后会把上面撑高，换行开关、文件被改都会改变布局，
 * 写回同一个像素就落到别的段落上。按行记录在这些变化后仍指向同一段文字，渲染与源码两个视图也共用它
 * （渲染视图按块级元素的 `data-source-line` 找行）。`offset` 只在同一视图里精确，换视图时对齐到这一行的开头附近。
 */
export type ReadingAnchor = { line: number; offset: number };

/** 文本开头。 */
export const READING_START: ReadingAnchor = { line: 1, offset: 0 };
