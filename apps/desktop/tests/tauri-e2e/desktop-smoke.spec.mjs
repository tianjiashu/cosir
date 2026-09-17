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

  it("hides the main window on close while keeping the backend alive", async () => {
    const backendBaseUrl = await browser.execute(
      () => window.__COSIR_RUNTIME_CONFIG__?.backendBaseUrl ?? null,
    );
    assert.match(String(backendBaseUrl), /^http:\/\/127\.0\.0\.1:\d+$/);

    const windowHandlesBefore = await browser.getWindowHandles();
    assert.equal(windowHandlesBefore.length, 1);

    await browser.execute(() =>
      window.__TAURI_INTERNALS__.invoke("e2e_close_main_window"),
    );
    await browser.waitUntil(
      async () => !(await browser.execute(() =>
        window.__TAURI_INTERNALS__.invoke("e2e_main_window_is_visible"),
      )),
      { timeout: 5000, timeoutMsg: "Tauri did not hide the main window on close" },
    );

    assert.deepEqual(await browser.getWindowHandles(), windowHandlesBefore);
    const healthResponse = await fetch(`${backendBaseUrl}/health`, { cache: "no-store" });
    assert.equal(healthResponse.status, 200);
  });
});
