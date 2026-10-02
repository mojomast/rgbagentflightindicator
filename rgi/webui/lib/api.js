// The only module that talks to fetch. Adds the token, normalises errors into
// ApiError, and never retries a 401 in a loop (the token dialog owns that).

let token = sessionStorage.getItem("rgi.token") || "";

export function getToken() { return token; }

export function setToken(value) {
  token = value || "";
  if (token) sessionStorage.setItem("rgi.token", token);
  else sessionStorage.removeItem("rgi.token");
}

export function clearToken() { setToken(""); }

export class ApiError extends Error {
  constructor(message, { status = 0, code = "", field = "", details = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.field = field;
    this.details = details;
  }
}

export async function api(path, { method = "GET", body, headers = {}, timeout = 8000 } = {}) {
  const hdrs = { ...headers };
  if (token) hdrs["X-LED-Token"] = token;
  if (body !== undefined) hdrs["Content-Type"] = "application/json";
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: hdrs,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeout),
    });
  } catch (err) {
    const code = err.name === "TimeoutError" ? "timeout" : "unreachable";
    throw new ApiError(
      err.name === "TimeoutError" ? `timed out after ${timeout} ms` : `panel unreachable (${err.message})`,
      { code });
  }
  const type = resp.headers.get("content-type") || "";
  let payload = null;
  try { payload = type.includes("json") ? await resp.json() : await resp.text(); }
  catch { payload = null; }
  if (!resp.ok) {
    const env = payload && payload.error ? payload.error : {};
    if (resp.status === 401) clearToken();
    throw new ApiError(env.message || `request failed (HTTP ${resp.status})`, {
      status: resp.status,
      code: env.code || `http_${resp.status}`,
      field: env.field || "",
      details: env.details || null,
    });
  }
  return payload;
}
