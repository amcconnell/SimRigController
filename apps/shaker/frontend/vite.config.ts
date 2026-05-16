import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// During `npm run dev`, proxy /api/* to the live Pi (override via VITE_API_TARGET).
// `npm run build` outputs to apps/shaker/src/shaker/web/static/, which FastAPI
// serves directly in production. No Node needed on the Pi.
const apiTarget = process.env.VITE_API_TARGET ?? "http://simrig-pi.local";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/shaker/web/static",
    emptyOutDir: true,
    assetsDir: "assets",
  },
  server: {
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
    },
  },
});
