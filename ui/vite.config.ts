import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the authoring service, so the app fetches from
// its own origin in development and in production alike. A base URL baked in at
// build time is a base URL that is wrong on the machine it is deployed to.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
