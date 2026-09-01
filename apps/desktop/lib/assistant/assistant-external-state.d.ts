/**
 * assistant-ui 运行时外部状态（ExternalState）的类型增强声明。
 *
 * 背景：在使用 `useAssistantTransportState` 读取后端透传的 transport state 时，
 * 该 hook 的 selector 入参类型为 `UserExternalState`，其定义为：
 *   keyof Assistant.ExternalState extends never
 *     ? Record<string, unknown>
 *     : Assistant.ExternalState[keyof Assistant.ExternalState]
 * 即：若未对 `Assistant.ExternalState` 做模块增强，则为模糊的 `Record<string, unknown>`，
 * 调用方只能靠 `as` 断言取得精确字段，编译期不校验、运行时无保护。
 *
 * 本文件通过 TypeScript 模块增强（module augmentation），把传输契约类型
 * `TransportState`（见同目录 `contract.ts`）注入 `Assistant.ExternalState`，
 * 使 `UserExternalState` 直接推导为 `TransportState`，从而：
 *   1. selector 入参 `s` 自动获得 `TransportState` 的精确类型，无需 `as` 断言；
 *   2. 后端 wire 结构保持不变（state 即 TransportState 本身，无需额外包裹层）。
 *
 * 文件位置要求：必须位于 `tsconfig.json` 的 `include` 范围内（本仓库为 `**\/*.ts`），
 * 且本文件含顶层 `import type` / `export {}`，属 ES 模块，保证增强被全局拾取。
 *
 * 官方推荐形态（assistant-ui 文档 /docs/runtimes/custom/assistant-transport 的
 * "Type safety" 章节）即为 `declare module "@assistant-ui/react" { namespace Assistant
 * { interface ExternalState {...} } }`，本文件的写法与其一致。
 */

// 引入传输契约类型，作为 ExternalState 增强字段的精确类型。
import type { TransportState } from "./contract";

// 标记为模块，确保 TypeScript 将其作为模块增强而非全局脚本处理。
export {};

declare module "@assistant-ui/react" {
  namespace Assistant {
    /**
     * 后端 assistant-transport 透传给前端的完整 state 结构。
     * 字段名 `transportState` 为自定义 key；值类型即后端 wire 的 TransportState。
     * 增强后 `UserExternalState` 直接推导为 `TransportState`，selector 入参 `s`
     * 即拥有 `messages` 与 `run` 等精确字段。
     */
    interface ExternalState {
      transportState: TransportState;
    }
  }
}
