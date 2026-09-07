import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies /api to the standalone migration server (default :8020).
// Override with OSMT_API_URL=http://host:port (env var or ui/.env file).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "OSMT_");
  return {
    plugins: [react()],
    server: {
      port: 5180,
      proxy: {
        "/api": { target: env.OSMT_API_URL || "http://127.0.0.1:8020", changeOrigin: true },
      },
    },
  };
});
