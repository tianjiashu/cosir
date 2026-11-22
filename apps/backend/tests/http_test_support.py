"""测试驱动的可信本机客户端身份；生产凭据仍只由 Electron 创建和持有。"""

TEST_CAPABILITY = "isolated-backend-test-capability"
TRUSTED_HEADERS = {"X-Cosir-Capability": TEST_CAPABILITY}
