import { testDraftModelConfig, type ModelConfigFields, type ModelConnectionFields } from "@/lib/api/model-connections";
import { userErrorMessage } from "@/lib/errors/user-error-message";

/** 同时在测的模型数：聚合站常按 Key 限流，一次全发容易整批被拒成「失败」。 */
const TEST_CONCURRENCY = 4;

export type ModelTestState =
  | { kind: "idle" }
  | { kind: "testing" }
  | { kind: "passed"; elapsedMs: number | null }
  | { kind: "failed"; message: string };

type DraftConnection = Pick<ModelConnectionFields, "base_url" | "api_key">;

/** 按「连接 + 模型 + 它的能力」做一次真实探活；单个模型的编辑器直接用它把关「测试通过才能保存」。 */
export async function testModel(connection: DraftConnection, model: ModelConfigFields): Promise<Extract<ModelTestState, { kind: "passed" | "failed" }>> {
  try {
    const result = await testDraftModelConfig({ ...connection, ...model });
    return result.success
      ? { kind: "passed", elapsedMs: result.elapsed_ms }
      : { kind: "failed", message: result.error_message };
  } catch (error) {
    return { kind: "failed", message: userErrorMessage(error, "连接测试失败，请重试") };
  }
}

/**
 * 逐个探活一批模型，最多 {@link TEST_CONCURRENCY} 个并发。
 *
 * 每个模型的开始与结果经 `onState` 立即回报，界面逐行显示进度，不等整批结束；单个失败不影响其余。
 * 返回通过测试的模型（保持输入顺序），调用方只保存这些——「测试通过才能保存」按模型把关。
 */
export async function testModelDrafts(
  connection: DraftConnection,
  models: readonly ModelConfigFields[],
  onState: (modelName: string, state: ModelTestState) => void,
): Promise<ModelConfigFields[]> {
  const passed = new Set<string>();
  let cursor = 0;
  const worker = async () => {
    while (cursor < models.length) {
      const model = models[cursor++]!;
      onState(model.model_name, { kind: "testing" });
      const state = await testModel(connection, model);
      if (state.kind === "passed") passed.add(model.model_name);
      onState(model.model_name, state);
    }
  };
  await Promise.all(Array.from({ length: Math.min(TEST_CONCURRENCY, models.length) }, worker));
  return models.filter((model) => passed.has(model.model_name));
}
