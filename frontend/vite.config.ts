import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // In development Vite serves the UI and forwards the API to uvicorn.
    // The built bundle is served by FastAPI itself, so no proxy applies there.
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
