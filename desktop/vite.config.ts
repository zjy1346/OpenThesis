import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  build: {
    // Tauri CSP intentionally disallows data: images; keep every provider logo as a local asset.
    assetsInlineLimit: 0,
  },
  server: {
    port: 1420,
    strictPort: true,
  },
  envPrefix: ["VITE_", "TAURI_ENV_"],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
  },
});
