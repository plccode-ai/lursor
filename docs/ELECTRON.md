# Lursor Desktop (Electron)

The Vite + React frontend in `frontend/` doubles as a desktop app via Electron.
In **packaged** builds the app owns its backend: it ships a frozen, self-contained
Python interpreter and starts/stops the FastAPI server itself. In **development**
it runs the backend from source via `uv` and loads the Vite dev server.

## Backend lifecycle (main process)

On launch (`electron/main.cjs`):

1. Pick a port (prefers `8791`, falls back to an ephemeral port if taken).
2. Show a splash window and spawn the backend:
   - **Packaged**: `<Resources>/backend/python/bin/python -m uvicorn app.main:app`,
     with `LURSOR_DATA_DIR=~/.lursor` so all writable state stays out of the
     read-only app bundle.
   - **Dev**: `uv run uvicorn app.main:app` from `../../backend` (no data-dir
     override, so dev keeps its existing DB next to the backend).
3. Poll `GET /api/health` until ok, then load the renderer (dev server URL or
   `dist/index.html`). Show an error screen if the backend never comes up.
4. The resolved API base (`http://127.0.0.1:<port>/api`) is passed to the renderer
   via `webPreferences.additionalArguments` and re-exposed by the preload as
   `window.electron.apiBase` — the port isn't known at build time.
5. The backend is spawned in its own process group and killed on quit
   (`before-quit`/`will-quit`), and a single-instance lock prevents port clashes.

## Layout

```
frontend/
  electron/
    main.cjs      Main process: backend lifecycle (spawn/health/teardown), window,
                  splash/error screens, external-link routing, single-instance lock.
    preload.cjs   contextIsolation bridge: window.electron = { isElectron, platform,
                  apiBase, openExternal }.
```

Key wiring:
- `vite.config.ts` sets `base: "./"` so built assets resolve under `file://`.
- `src/main.tsx` uses `HashRouter` when `window.electron?.isElectron` is set
  (history routing doesn't work from `file://`), and `BrowserRouter` in the browser.
- `src/api/client.ts` prefers `window.electron.apiBase` when present.
- `src/vite-env.d.ts` declares the `window.electron` type.

## Develop

```bash
cd frontend
bun install
bun run electron:dev     # starts Vite, waits for :8899, then launches Electron
```

Make sure the backend is running (`http://localhost:8791`).

## Package

Packaging freezes the backend into a self-contained bundle first, then builds the
Electron app with that bundle under its resources:

```bash
cd frontend
bun run bundle:backend            # freeze the backend -> ../backend/bundle (heavy, ~400 MB)
bun run electron:build            # current platform (runs bundle:backend for you)
bun run electron:build:mac        # or :linux  (each runs bundle:backend first)
```

- `backend/scripts/build_bundle.sh` produces the frozen backend: a relocatable
  standalone CPython (via `uv`) with the backend + all deps installed into it,
  then smoke-tests a real `uvicorn` boot + `/api/health`.
- electron-builder copies `backend/bundle` into the app's resources as `backend`
  (`extraResources` in `frontend/package.json`).
- Artifacts land in `frontend/release/`. Config lives in the `build` block of
  `frontend/package.json` (appId `com.lursor.app`).

The frozen backend is **architecture-specific**, so a single machine builds for
its own arch. CI (`.github/workflows/release.yml`) builds each arch on a matching
runner: macOS arm64 (`macos-15`) and Linux x64 (`ubuntu-latest`). Intel macOS is
not built (see [DISTRIBUTION.md](./DISTRIBUTION.md)).

## Signing and notarization (macOS)

Released macOS builds are signed with a Developer ID certificate and notarized.
Two pieces make that work with a bundled interpreter:

- `build/entitlements.mac.plist` — the hardened runtime forbids most of what this
  app does by default. JIT, unsigned executable memory, library validation, and
  network client/server are all required by Electron or CPython; each key in the
  file says which.
- `scripts/sign-backend-bundle.cjs` — an `afterPack` hook that discovers every
  Mach-O file under `Resources/backend` and codesigns it. Notarization requires
  *every* nested binary to be signed, but `@electron/osx-sign` only signs
  Electron's own binaries plus an explicit `mac.binaries` list — which would rot
  on every dependency bump, so we discover them instead. It runs at `afterPack`
  because signatures apply inside-out: the app's own signature seals the contents
  of `Resources/`.

Local builds without a certificate still work — the hook logs that it found no
identity and leaves the bundle unsigned.

Auto-update is wired in `main.cjs` and covers the Linux AppImage and signed macOS
builds via `electron-updater`. Unsigned macOS builds — which Squirrel.Mac refuses
to update — fall back to `scripts/update.sh`, run from Terminal after the app
quits. See [DISTRIBUTION.md](./DISTRIBUTION.md) for that fallback, the release
runbook, the CI secrets, and the Homebrew tap.

Windows remains unbuilt.
