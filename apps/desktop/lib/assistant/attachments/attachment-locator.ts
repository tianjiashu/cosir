/**
 * 图片附件 locator 契约的唯一定义：`cosir-attachment://<asset id>`，asset id 为 64 位小写十六进制
 *（后端按内容寻址的附件 id）。快照校验、工具卡片的展示读取与图片地址解析都按这里判定。
 */
export const LOCAL_IMAGE_LOCATOR_PREFIX = "cosir-attachment://";

const ASSET_ID_SOURCE = "[0-9a-f]{64}";

export const ATTACHMENT_ASSET_ID = new RegExp(`^${ASSET_ID_SOURCE}$`);

/** 匹配完整的图片 locator，分组 1 为 asset id。 */
export const IMAGE_LOCATOR = new RegExp(`^${LOCAL_IMAGE_LOCATOR_PREFIX}(${ASSET_ID_SOURCE})$`);
