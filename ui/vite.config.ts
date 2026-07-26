import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build lands inside the Python package so one container can serve the API
// and the console from the same origin. That keeps the deployment to a single
// process and avoids CORS in production entirely.
export default defineConfig({
  plugins: [react()],
  // Absolute, not relative: the SPA fallback serves index.html for client routes
  // like /waves, and a relative asset path would resolve against that path.
  base: "/",
  build: {
    outDir: "../src/ucm_bridge/api/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
