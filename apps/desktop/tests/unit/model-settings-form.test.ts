import { describe, expect, it } from "vitest";

import {
  modelSettingsFromForm,
  modelSettingsToForm,
  type ModelSettingsForm,
} from "@/lib/model-settings-form";

describe("model settings form conversion", () => {
  it("将后端 JSON 覆盖项回填为结构化表单值", () => {
    expect(modelSettingsToForm({
      temperature: 0.2,
      top_p: 0.8,
      max_tokens: 4096,
      thinking: true,
      provider_type: "deepseek",
      drop_params: false,
      stream: true,
      reasoning_effort: "high",
      response_format: "json_object",
    })).toEqual({
      temperature: "0.2",
      top_p: "0.8",
      max_tokens: "4096",
      thinking: "true",
      provider_type: "deepseek",
      drop_params: "false",
      stream: "true",
      reasoning_effort: "high",
      response_format: "json_object",
    });
  });

  it("前端隐藏的配置字段在读取后再次保存时保持不变", () => {
    const settings = {
      stream: true,
      response_format: "json_object",
      provider_type: "deepseek",
      drop_params: false,
    };

    expect(modelSettingsFromForm(modelSettingsToForm(settings))).toEqual(settings);
  });

  it("将结构化表单值转换为后端 JSON，并省略未覆盖字段", () => {
    const form: ModelSettingsForm = {
      temperature: "",
      top_p: "0.8",
      max_tokens: "4096",
      thinking: "true",
      provider_type: "",
      drop_params: "",
      stream: "true",
      reasoning_effort: "high",
      response_format: "",
    };

    expect(modelSettingsFromForm(form)).toEqual({
      top_p: 0.8,
      max_tokens: 4096,
      thinking: true,
      stream: true,
      reasoning_effort: "high",
    });
  });

  it("拒绝不能转换为模型参数的数字", () => {
    const form = modelSettingsToForm({});
    expect(() => modelSettingsFromForm({ ...form, temperature: "abc" })).toThrow("temperature");
    expect(() => modelSettingsFromForm({ ...form, max_tokens: "1.5" })).toThrow("max_tokens");
  });
});
