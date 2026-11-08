/**
 * 长用户消息收起时的尺寸，气泡渲染（`UserMessageBubble`）与虚拟列表估高共用，改一处两边一致。
 *
 * 收起高度约 10 行正文：一般的需求描述原样完整显示，贴整段规则、日志这类才会收起。
 */
export const USER_MESSAGE_CLAMP_HEIGHT = 240;

/** 「显示全部 / 收起」一行：`mt-1` + `h-6`。 */
export const USER_MESSAGE_TOGGLE_HEIGHT = 28;
