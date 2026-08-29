/**
 * 后端 API 共享类型定义。
 *
 * 本文件由 `scripts/generate_api_ts.py` 从后端 Pydantic API schema 生成。
 * 不要手动修改；请先更新 `apps/backend/app/api/schemas/` 后重新生成。
 *
 * @module shared/attachment
 */
export type AttachmentKind = 'image' | 'file' | 'directory' | 'url';

export interface AttachmentRef {
  kind: AttachmentKind;
  ref: string;
}
