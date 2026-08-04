function resolveApiBase(): string {
  // In the desktop app the backend runs on a port chosen at launch (possibly
  // ephemeral), so the main process hands the resolved base to the renderer via
  // the preload. This always wins when present.
  const fromElectron =
    typeof window !== "undefined" ? window.electron?.apiBase : undefined
  if (fromElectron) return fromElectron

  const configured = import.meta.env.VITE_API_BASE as string | undefined

  // Single-origin server deployment (this fork's container image): the SPA and
  // the API are served from one origin, a reverse proxy forwards /api to the
  // backend. Build with VITE_API_BASE="same-origin" and the base is derived from
  // the page origin at runtime — an ABSOLUTE URL (the WebSocket helpers call
  // `new URL()` on it, so it must be absolute) that works behind ANY hostname
  // (Cloudflare, per-tenant subdomains) with no rebuild. wss:// is derived from
  // the https page automatically.
  if (configured === "same-origin") {
    return typeof window !== "undefined" ? `${window.location.origin}/api` : "/api"
  }

  // An explicit absolute VITE_API_BASE wins as-is (no LAN rewrite below).
  if (configured) return configured

  // Dev default: no VITE_API_BASE. The Vite dev server (:8899) and the API
  // (:8791) are separate origins. When the app is opened from another device
  // over the LAN (e.g. a phone at http://192.168.x.x:8899), a hardcoded
  // `localhost` would resolve to that device instead of the machine running the
  // API. Talk to the API on whichever host served the page, port 8791. Localhost
  // and Electron (file://) fall through to the localhost default.
  if (typeof window !== "undefined" && window.location.protocol.startsWith("http")) {
    const host = window.location.hostname
    if (host !== "localhost" && host !== "127.0.0.1") {
      return `${window.location.protocol}//${host}:8791/api`
    }
  }
  return "http://localhost:8791/api"
}

export const API_BASE: string = resolveApiBase()

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(message: string, status: number, body: unknown) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.body = body
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal } = options

  // FormData is sent as multipart; let the browser set the boundary header and
  // pass the body through unserialized. Everything else goes as JSON.
  const isForm = typeof FormData !== "undefined" && body instanceof FormData

  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers:
      body !== undefined && !isForm
        ? { "Content-Type": "application/json" }
        : undefined,
    body: body !== undefined ? (isForm ? (body as FormData) : JSON.stringify(body)) : undefined,
    signal,
  })

  if (!res.ok) {
    let parsed: unknown = null
    try {
      parsed = await res.json()
    } catch {
      parsed = await res.text().catch(() => null)
    }
    const message =
      parsed && typeof parsed === "object" && "detail" in parsed
        ? String((parsed as { detail: unknown }).detail)
        : `Request failed with status ${res.status}`
    throw new ApiError(message, res.status, parsed)
  }

  if (res.status === 204) {
    return undefined as T
  }

  const text = await res.text()
  if (!text) {
    return undefined as T
  }
  return JSON.parse(text) as T
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  post: <T>(path: string, body: unknown, signal?: AbortSignal) =>
    request<T>(path, { method: "POST", body, signal }),
  upload: <T>(path: string, form: FormData, signal?: AbortSignal) =>
    request<T>(path, { method: "POST", body: form, signal }),
  put: <T>(path: string, body: unknown, signal?: AbortSignal) =>
    request<T>(path, { method: "PUT", body, signal }),
  patch: <T>(path: string, body: unknown, signal?: AbortSignal) =>
    request<T>(path, { method: "PATCH", body, signal }),
  delete: <T>(path: string, signal?: AbortSignal) =>
    request<T>(path, { method: "DELETE", signal }),
}
