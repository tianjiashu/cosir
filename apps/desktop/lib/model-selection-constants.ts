/**
 * Reasoning effort 领域常量（合法取值单一来源）。
 *
 * 与 localStorage 读写无关，独立成中性模块，避免表现层组件为取一个常量而
 * 耦合 model-selection-storage 的存储基础设施。UI 选项、类型守卫与适配器均
 * 引用本常量。
 */
export const REASONING_EFFORT_VALUES = ["low", "high", "max"] as const;
