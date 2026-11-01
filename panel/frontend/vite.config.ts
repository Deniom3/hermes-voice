import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// /vnc и /vnc-ws уходят на голосовой сервер (8792), чтобы iframe и noVNC
// жили на одном origin со страницей — тогда https не поломает http-вставку.
export default defineConfig({
  plugins: [react()],
  // Сборка кладётся рядом с server.py: он отдаёт DIST = Path(__file__).with_name("dist").
  build: {
    outDir: "../dist",
    emptyOutDir: true,
  },
  server: {
    host: "0.0.0.0",
    port: 8791,
    allowedHosts: true,
    proxy: {
      "/vnc": { target: "http://127.0.0.1:8792", changeOrigin: true },
      "/vnc-ws": { target: "ws://127.0.0.1:8793", ws: true, rewrite: () => "/vnc-ws" },
      "/health": { target: "http://127.0.0.1:8792", changeOrigin: true },
    },
  },
});
