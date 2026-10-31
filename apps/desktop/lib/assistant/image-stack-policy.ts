/**
 * 一条用户消息的图片超过这个数量就收成一叠（`ImageStack`），否则逐张铺开。
 *
 * 只是界面阈值，不是后端事实；渲染（`UserMessageImages`）与虚拟列表行高估计（`message-height-estimate`）
 * 共用这一个值，两边不一致时估计会偏差一整块图片区的高度，滚动时被逐行修正。
 */
const IMAGE_STACK_THRESHOLD = 5;

export function shouldStackImages(count: number): boolean {
  return count > IMAGE_STACK_THRESHOLD;
}
