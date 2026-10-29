export class ApiError extends Error {
  constructor(message, status = 0, code = 0) { super(message); this.name = 'ApiError'; this.status = status; this.code = code; }
}

export class ApiClient {
  constructor({ fetcher = globalThis.fetch.bind(globalThis), storage = globalThis.sessionStorage, onExpire = () => {}, onChange = () => {} } = {}) {
    this.fetcher = fetcher; this.storage = storage; this.onExpire = onExpire; this.onChange = onChange; this.refreshing = null; this.sessionVersion = 0;
    try { this.auth = JSON.parse(storage?.getItem('qixue-auth-v1') || 'null'); } catch { this.auth = null; }
  }
  save(value, { rotating = false } = {}) { if (!rotating) this.sessionVersion++; this.auth = value; this.onChange(value); try { value ? this.storage?.setItem('qixue-auth-v1', JSON.stringify(value)) : this.storage?.removeItem('qixue-auth-v1'); } catch { /* Memory-only session remains usable. */ } }
  get loggedIn() { return Boolean(this.auth?.token?.access_token); }
  async refresh() {
    if (!this.auth?.token?.refresh_token) return false;
    if (!this.refreshing) this.refreshing = (async () => {
      const old = this.auth;
      try {
        const next = await this.json('/auth/refresh', { method: 'POST', body: { refresh_token: old.token.refresh_token }, authenticated: false });
        if (this.auth !== old) return false;
        this.save(next, { rotating: true }); return true;
      } catch (error) {
        // A transient server/network failure must not destroy a valid refresh token.
        if (this.auth === old && error.status === 401) { this.save(null); this.onExpire(); }
        throw error;
      }
    })().finally(() => { this.refreshing = null; });
    return this.refreshing;
  }
  async request(path, { method = 'GET', body, headers = {}, signal, authenticated = true, retry = true } = {}) {
    const session = this.sessionVersion, accessToken = this.auth?.token?.access_token;
    const h = { ...headers }; if (body !== undefined) h['Content-Type'] = 'application/json';
    if (authenticated && this.auth?.token?.access_token) h.Authorization = `Bearer ${this.auth.token.access_token}`;
    let response;
    try { response = await this.fetcher('/api/v1' + path, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body), signal }); }
    catch (error) { if (error.name === 'AbortError') throw error; throw new ApiError('连接中断，请检查后端服务后重试。'); }
    if (authenticated && session !== this.sessionVersion) throw new ApiError('登录状态已变化，请重新操作。', 409);
    if (response.status === 401 && authenticated && retry && this.auth?.token?.refresh_token) {
      // Another request may already have rotated this access token while we were waiting.
      if (accessToken !== this.auth.token.access_token || await this.refresh()) return this.request(path, { method, body, headers, signal, authenticated, retry: false });
      if (session !== this.sessionVersion) throw new ApiError('登录状态已变化，请重新操作。', 409);
    }
    if (!response.ok) {
      let data; try { data = await response.json(); } catch { /* Non-JSON proxy response. */ }
      if (response.status === 401 && authenticated) { this.save(null); this.onExpire(); }
      throw new ApiError(data?.message || `请求未完成（${response.status}）`, response.status, data?.code);
    }
    return response;
  }
  async json(path, options) {
    const session = this.sessionVersion;
    const response = await this.request(path, options);
    let envelope; try { envelope = await response.json(); } catch { throw new ApiError('服务器返回了无法识别的数据。'); }
    if (options?.authenticated !== false && session !== this.sessionVersion) throw new ApiError('登录状态已变化，请重新操作。', 409);
    if (envelope.code !== 0) throw new ApiError(envelope.message || '请求未完成', response.status, envelope.code);
    return envelope.data;
  }
  async logout() {
    const token = this.auth?.token?.refresh_token;
    // Revoke first; do not report a successful server logout when the backend failed.
    if (token) await this.json('/auth/logout', { method: 'POST', body: { refresh_token: token }, authenticated: false });
    this.save(null);
  }
}

// Streaming UTF-8 decoding; frames may split at any byte, including inside Chinese text.
export async function readSSE(response, onEvent, { signal } = {}) {
  if (!response.headers.get('content-type')?.includes('text/event-stream')) throw new ApiError('服务器未返回流式回答，请检查接口。');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let pending = '', event = 'message', data = [], id, stopped = false;
  const dispatch = async () => {
    if (data.length) stopped = (await onEvent({ event, data: data.join('\n'), id })) === false;
    event = 'message'; data = []; id = undefined;
  };
  const line = async text => {
    if (text === '') return dispatch();
    if (text.startsWith(':')) return;
    const at = text.indexOf(':'), field = at < 0 ? text : text.slice(0, at);
    let value = at < 0 ? '' : text.slice(at + 1); if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
    else if (field === 'id' && !value.includes('\0')) id = value;
  };
  try {
    while (!stopped) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      const { value, done } = await reader.read(); pending += decoder.decode(value, { stream: !done });
      let match;
      while (!stopped && (match = /\r\n|\n|\r/.exec(pending))) {
        // A CR at the chunk boundary may be the first half of CRLF.
        if (!done && match[0] === '\r' && match.index === pending.length - 1) break;
        const text = pending.slice(0, match.index); pending = pending.slice(match.index + match[0].length); await line(text);
      }
      if (done) { if (pending) await line(pending); if (data.length) await dispatch(); break; }
    }
  } finally { try { await reader.cancel(); } catch { /* Already aborted. */ } reader.releaseLock(); }
}
