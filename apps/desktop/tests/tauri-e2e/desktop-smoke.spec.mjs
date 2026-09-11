import assert from "node:assert/strict";

describe("Tauri desktop baseline", () => {
  it("starts the Rust host and reaches the local workspace shell", async () => {
    assert.equal(await browser.getTitle(), "Cosir");

    const startupMessage = await $("//*[contains(normalize-space(), '本地 Agent 正在启动')]");
    await startupMessage.waitForDisplayed({ reverse: true, timeout: 120000 });

    await expect($("//*[normalize-space()='工作区']")).toBeDisplayed();
    await expect($("//*[normalize-space()='新对话']")).toBeDisplayed();
    await expect($("nav[aria-label='工作区任务列表']")).toBeDisplayed();

    const backendBaseUrl = await browser.execute(() => window.__COSIR_RUNTIME_CONFIG__?.backendBaseUrl ?? null);
    assert.match(String(backendBaseUrl), /^http:\/\/127\.0\.0\.1:\d+$/);
  });
});
