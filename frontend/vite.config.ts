import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

import pkg from "./package.json" with { type: "json" }

// https://vite.dev/config/
export default defineConfig({
  // The app version, inlined at build time. package.json is the single source
  // of truth — electron-builder names every release artifact from the same
  // field, so what the UI shows always matches the build it shipped in.
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  // Relative asset paths so the built app loads correctly from file:// inside
  // Electron (the browser build is unaffected — the dev server ignores base).
  base: "./",
  // Expose the dev server on the LAN (0.0.0.0) so it's reachable from other
  // devices on the network, not just localhost.
  //
  // Port 8899 is deliberately off the common web dev-server ports (3000, 5173,
  // 8000, 8080) that Lursor workspaces spin up, so a workspace's dev server
  // can't shadow the Lursor UI. strictPort keeps the UI on a known, memorable
  // port instead of silently bumping to the next free one.
  // (Moved off upstream's :8888 in this fork so it can run next to the PLCcode
  // admin app, whose dev server owns :8888.)
  server: { host: true, port: 8899, strictPort: true },
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
})
