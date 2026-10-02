// Calls to the local server. The page was served with a per-start token that state-changing calls must echo.

const token = document.querySelector('meta[name="eri-token"]')?.content ?? "";

export class ApiError extends Error {
  constructor(status, code, message, extra = {}) {
    super(message);
    this.status = status; this.code = code; this.extra = extra;
  }
}

const NETWORK = "無法連線到本機服務。請確認啟動程式的黑色視窗還開著，然後重新整理這個頁面。";

async function request(method, url, body) {
  const init = { method, headers: { "X-ERI-Token": token } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try { res = await fetch(url, init); } catch { throw new ApiError(0, "NETWORK", NETWORK); }
  return parse(res);
}

async function parse(res) {
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  if (!res.ok) {
    const e = data?.error ?? {};
    throw new ApiError(res.status, e.code ?? "HTTP_" + res.status, e.message ?? `伺服器回應錯誤（${res.status}）`, e);
  }
  return data;
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body = {}) => request("POST", url, body),
  put: (url, body) => request("PUT", url, body),
  patch: (url, body) => request("PATCH", url, body),
  del: (url) => request("DELETE", url),
  // multipart upload with progress (fetch cannot report upload progress)
  upload(url, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.setRequestHeader("X-ERI-Token", token);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onerror = () => reject(new ApiError(0, "NETWORK", NETWORK));
      xhr.onload = () => parse(new Response(xhr.responseText, { status: xhr.status })).then(resolve, reject);
      const form = new FormData();
      form.append("file", file, file.name);
      xhr.send(form);
    });
  },
};
