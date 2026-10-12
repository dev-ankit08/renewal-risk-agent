import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Built into the Python package so `renewal-risk serve` serves it at /admin/.
export default defineConfig({
  base: "/admin/",
  plugins: [react()],
  build: { outDir: "../renewal_risk/admin/static", emptyOutDir: true },
  server: { proxy: { "/admin/api": "http://127.0.0.1:8080", "/admin/oauth": "http://127.0.0.1:8080", "/v1": "http://127.0.0.1:8080" } },
});
