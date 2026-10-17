import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: ".",
  use: { baseURL: "http://127.0.0.1:4183" },
  webServer: {
    cwd: "..",
    command: "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 4183",
    url: "http://127.0.0.1:4183",
    reuseExistingServer: false,
  },
});
