/* 后端请求封装:统一把 FastAPI 的 {detail} 错误转成 Error。 */

async function request(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) {
    let msg = data.detail;
    // 422 校验错误的 detail 是数组
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg || JSON.stringify(d)).join(';');
    throw new Error(msg || `请求失败(HTTP ${res.status})`);
  }
  return data;
}

export const api = {
  get: (url) => request('GET', url),
  post: (url, body) => request('POST', url, body),
  patch: (url, body) => request('PATCH', url, body),
  del: (url) => request('DELETE', url),
};
