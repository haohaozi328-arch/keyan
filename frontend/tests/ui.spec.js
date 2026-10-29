import { afterEach, expect, test } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';
import App from '../src/App.vue';
import MarkdownContent from '../src/components/MarkdownContent.vue';

const user = { id: '373814368841760768', email: 'student@example.com', nickname: '同学' };
const auth = { user, token: { access_token: 'access-old', refresh_token: 'refresh-old' } };
const conv = { id: '473814368841760768', title: '测试会话' };
const reply = (data, status = 200) => new Response(JSON.stringify({ code: status < 400 ? 0 : 22410, message: '测试响应', data }), { status, headers: { 'Content-Type': 'application/json' } });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const stream = frames => new Response(frames, { headers: { 'Content-Type': 'text/event-stream' } });
const start = 'event: start\ndata: {"conversation_id":"473814368841760768","message_id":"573814368841760768"}\n\n';
const delta = (text, id = '1-0') => `id: ${id}\nevent: delta\ndata: ${JSON.stringify({ delta: text })}\n\n`;
const done = 'event: done\nid: 99-0\ndata: [DONE]\n\n';
const wrappers = [];
afterEach(() => { wrappers.forEach(w => w.unmount()); wrappers.length = 0; sessionStorage.clear(); document.body.innerHTML = ''; });

async function setup(route = () => undefined, signedIn = true) {
  const fetcher = async (url, options = {}) => {
    const custom = await route(url, options); if (custom !== undefined) return custom;
    if (url === '/frontend-config.json') return Response.json({ captchaEnabled: false, captchaVid: '' });
    if (url.endsWith('/health/ready')) return reply({ status: 'ok' });
    if (url.endsWith('/chat/status')) return reply({ configured: true, model: 'test' });
    if (url.endsWith('/auth/me')) return reply(user);
    if (url.includes('/conversations?')) return reply({ items: [conv], next_cursor: null });
    if (url.endsWith('/auth/logout')) return reply(null);
    throw new Error('Unhandled test request: ' + url);
  };
  if (signedIn) sessionStorage.setItem('qixue-auth-v1', JSON.stringify(auth));
  const wrapper = mount(App, { props: { clientOptions: { fetcher, storage: sessionStorage } }, attachTo: document.body });
  wrappers.push(wrapper); await wrapper.vm.ready(); await flushPromises();
  return { wrapper, chat: wrapper.vm.chat };
}

test('Vue form opens login for anonymous questions, normalizes email and loads account history', async () => {
  const calls = [];
  const { wrapper, chat } = await setup((url, options) => { calls.push({ url, body: options.body && JSON.parse(options.body) }); if (url.endsWith('/auth/login')) return reply(auth); }, false);
  await wrapper.get('#question').setValue('未登录问题');
  await wrapper.get('#composer').trigger('submit');
  expect(wrapper.get('#authDialog').element.open).toBe(true);
  expect(calls.some(c => c.url.endsWith('/chat/stream'))).toBe(false);
  await wrapper.get('#email').setValue('Student@Example.COM');
  await wrapper.get('#password').setValue('test-password');
  await wrapper.get('#authForm').trigger('submit'); await flushPromises();
  expect(calls.find(c => c.url.endsWith('/auth/login')).body.email).toBe('student@example.com');
  expect(wrapper.get('#authDialog').element.open).toBe(false);
  expect(chat.loggedIn.value).toBe(true);
  expect(wrapper.get('.account-text strong').text()).toBe('同学');
  expect(wrapper.findAll('.history-item')).toHaveLength(1);
});

test('Vue messages react to SSE deltas; feedback, rename and delete use existing backend contracts', async () => {
  const calls = []; let title = conv.title, deleted = false;
  const { wrapper, chat } = await setup((url, options) => {
    const body = options.body && JSON.parse(options.body); calls.push({ url, method: options.method, body });
    if (url.includes('/conversations?')) return reply({ items: deleted ? [] : [{ ...conv, title }], next_cursor: null });
    if (url.endsWith('/chat/stream')) return stream(start + delta('请先检查输入条件。') + done);
    if (url.endsWith('/feedback')) return reply({ rating: body.rating });
    if (options.method === 'PATCH') { title = body.title; return reply({ ...conv, title }); }
    if (options.method === 'DELETE') { deleted = true; return reply(null); }
  });
  await wrapper.get('#question').setValue('如何调试？');
  await wrapper.get('#composer').trigger('submit'); await flushPromises();
  expect(wrapper.get('.message-body').text()).toBe('请先检查输入条件。');
  expect(chat.state.messages.at(-1).status).toBe('complete');
  expect(calls.find(c => c.url.endsWith('/chat/stream')).body).toEqual({ message: expect.stringContaining('逐步提示') });
  await wrapper.get('[aria-label="有帮助"]').trigger('click'); await flushPromises();
  expect(wrapper.get('[aria-label="有帮助"]').attributes('aria-pressed')).toBe('true');
  await wrapper.get('[aria-label="有帮助"]').trigger('click'); await flushPromises();
  expect(chat.state.messages.at(-1).feedback).toBeNull();
  await wrapper.get('[aria-label^="重命名："]').trigger('click');
  await wrapper.get('#editInput').setValue('调试笔记');
  await wrapper.get('#editForm').trigger('submit'); await flushPromises();
  expect(wrapper.get('#conversationTitle').text()).toBe('调试笔记');
  await wrapper.get('[aria-label^="删除："]').trigger('click');
  await wrapper.get('#confirmDelete').trigger('click'); await flushPromises();
  expect(wrapper.find('#welcome').exists()).toBe(true);
  expect(wrapper.findAll('.history-item')).toHaveLength(0);
});

test('Expired resume retains partial history and removes the resume button', async () => {
  const { wrapper, chat } = await setup(url => {
    if (url.includes('/messages?')) return reply({ items: [{ id: '573814368841760768', role: 'assistant', content: '已经生成的部分回答', status: 'streaming' }], next_before_seq: null });
    if (url.includes('/chat/stream/')) return reply(null, 410);
  });
  await chat.openConversation(conv); await nextTick();
  await wrapper.get('#resumeBtn').trigger('click'); await flushPromises();
  expect(wrapper.get('.message-body').text()).toBe('已经生成的部分回答');
  expect(wrapper.get('#statusText').text()).toContain('过期');
  expect(wrapper.find('#resumeBtn').exists()).toBe(false);
});

test('SSE terminal error ends the turn without replaying the same error', async () => {
  const { chat, wrapper } = await setup(url => url.endsWith('/chat/stream') ? stream(start + 'event: error\nid: 3-0\ndata: {"code":22502,"message":"上游暂时不可用"}\n\n') : undefined);
  await chat.submitQuestion('你好'); await nextTick();
  expect(chat.state.pending).toBeNull();
  expect(chat.state.messages.at(-1).status).toBe('failed');
  expect(wrapper.find('#resumeBtn').exists()).toBe(false);
});

test('Logout prevents a late message response from restoring private history', async () => {
  const pending = deferred();
  const { wrapper, chat } = await setup(url => url.includes('/messages?') ? pending.promise : undefined);
  const opening = chat.openConversation(conv);
  await chat.logout();
  pending.resolve(reply({ items: [{ role: 'assistant', content: '退出后不应显示', status: 'complete' }], next_before_seq: null }));
  await opening; await nextTick();
  expect(chat.state.messages).toHaveLength(0);
  expect(chat.state.current).toBeNull();
  expect(wrapper.text()).not.toContain('退出后不应显示');
  expect(wrapper.findAll('.history-item')).toHaveLength(0);
});

test('Interrupted streaming uses GET and Last-Event-ID with no second generation POST', async () => {
  const calls = [];
  const { chat, wrapper } = await setup((url, options) => {
    if (!url.includes('/chat/stream')) return;
    calls.push({ url, ...options });
    return options.method === 'POST' ? stream(start + delta('第一段', '10-0')) : stream(start + delta('第二段', '11-0') + done);
  });
  await chat.submitQuestion('请分步解释'); await nextTick();
  expect(wrapper.get('.message-body').text()).toBe('第一段第二段');
  expect(calls).toHaveLength(2);
  expect(calls[1].method).toBe('GET');
  expect(calls[1].headers['Last-Event-ID']).toBe('10-0');
  expect(calls.filter(c => c.method === 'POST')).toHaveLength(1);
});

test('History and message cursors preserve existing rows in the rendered Vue list', async () => {
  const urls = [];
  const { chat, wrapper } = await setup(url => {
    urls.push(url);
    if (url.includes('/conversations?')) return reply({ items: url.includes('cursor=') ? [{ id: '2', title: '更早会话' }] : [conv], next_cursor: url.includes('cursor=') ? null : 'page+/=' });
    if (url.includes('/messages?')) return reply({ items: [{ id: url.includes('before_seq=') ? 'old' : 'recent', role: 'user', content: '历史问题', status: 'complete' }], next_before_seq: url.includes('before_seq=') ? null : 21 });
  });
  await wrapper.get('#moreHistory').trigger('click'); await flushPromises();
  expect(wrapper.findAll('.history-item')).toHaveLength(2);
  expect(urls.some(u => u.includes('cursor=page%2B%2F%3D'))).toBe(true);
  await chat.openConversation(conv); await nextTick();
  await wrapper.get('#olderMessages').trigger('click'); await flushPromises();
  expect(chat.state.messages.map(m => m.id)).toEqual(['old', 'recent']);
  expect(urls.some(u => u.includes('before_seq=21'))).toBe(true);
});

test('Safe Markdown component never executes model HTML or unsafe links', async () => {
  const wrapper = mount(MarkdownContent, { props: { content: '<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1)) [教材](https://example.com)\n\n```html\n<script>alert(1)</script>\n```' } });
  wrappers.push(wrapper); await nextTick();
  expect(wrapper.findAll('img,script')).toHaveLength(0);
  expect(wrapper.findAll('a')).toHaveLength(1);
  expect(wrapper.get('a').attributes('href')).toBe('https://example.com');
  expect(wrapper.get('pre code').text()).toContain('<script>');
  await wrapper.get('.code-head button').trigger('click');
  expect(wrapper.emitted('copy')[0][0]).toBe('<script>alert(1)</script>');
});

test('Suggestions only fill a draft; mobile drawer and Escape update inert and expanded state', async () => {
  const { wrapper, chat } = await setup(undefined, false);
  await wrapper.get('.suggestion').trigger('click');
  expect(wrapper.get('#question').element.value).toContain('代码');
  expect(chat.state.messages).toHaveLength(0);
  chat.state.mobile = true; await nextTick();
  await wrapper.get('#menuBtn').trigger('click');
  expect(wrapper.get('#menuBtn').attributes('aria-expanded')).toBe('true');
  expect(wrapper.get('main').attributes()).toHaveProperty('inert');
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); await nextTick();
  expect(wrapper.get('#menuBtn').attributes('aria-expanded')).toBe('false');
  expect(wrapper.find('#scrim').exists()).toBe(false);
});

test('Registration sends email verification and account fields without model calls', async () => {
  const calls = [];
  const { wrapper, chat } = await setup((url, options) => {
    calls.push({ url, body: options.body && JSON.parse(options.body) });
    if (url.endsWith('/auth/verification-code')) return reply({ resend_after: 60 });
    if (url.endsWith('/auth/register')) return reply(auth, 201);
  }, false);
  await wrapper.get('#accountBtn').trigger('click'); await wrapper.get('#registerTab').trigger('click');
  await wrapper.get('#email').setValue('Student@Example.COM');
  await wrapper.get('#sendCode').trigger('click'); await flushPromises();
  expect(calls.find(c => c.url.endsWith('/verification-code')).body).toEqual({ email: 'student@example.com', scene: 'register' });
  expect(chat.state.codeUntil).toBeGreaterThan(Date.now());
  await wrapper.get('#password').setValue('test-password'); await wrapper.get('#nickname').setValue('同学'); await wrapper.get('#emailCode').setValue('123456');
  await wrapper.get('#authForm').trigger('submit'); await flushPromises();
  expect(calls.find(c => c.url.endsWith('/auth/register')).body).toEqual({ email: 'student@example.com', password: 'test-password', nickname: '同学', code: '123456' });
  expect(chat.loggedIn.value).toBe(true);
});

test('An open stream renders immediately; stop and resume keep one generation request', async () => {
  const calls = [];
  const { chat, wrapper } = await setup((url, options) => {
    if (!url.includes('/chat/stream')) return;
    calls.push({ url, ...options });
    if (options.method === 'GET') return stream(delta('后续内容', '21-0') + done);
    return new Response(new ReadableStream({ start(c) {
      c.enqueue(new TextEncoder().encode(start + delta('实时内容', '20-0')));
      options.signal.addEventListener('abort', () => c.error(new DOMException('Aborted', 'AbortError')), { once: true });
    } }), { headers: { 'Content-Type': 'text/event-stream' } });
  });
  const generating = chat.submitQuestion('保持连接'); await flushPromises();
  expect(chat.state.busy).toBe(true);
  expect(wrapper.get('.message-body').text()).toBe('实时内容');
  expect(wrapper.get('#sendBtn').attributes('aria-label')).toBe('停止接收回答');
  await wrapper.get('#composer').trigger('submit'); await generating; await nextTick();
  expect(chat.state.busy).toBe(false);
  await wrapper.get('#resumeBtn').trigger('click'); await flushPromises();
  expect(wrapper.get('.message-body').text()).toBe('实时内容后续内容');
  expect(calls.filter(c => c.method === 'POST')).toHaveLength(1);
  expect(calls[1].headers['Last-Event-ID']).toBe('20-0');
});

test('Captcha on an HTTP page gives a clear message and never sends an unverified code request', async () => {
  const calls = [];
  const { wrapper } = await setup(url => {
    calls.push(url);
    if (url === '/frontend-config.json') return Response.json({ captchaEnabled: true, captchaVid: 'test-vid' });
  }, false);
  await wrapper.get('#accountBtn').trigger('click'); await wrapper.get('#registerTab').trigger('click');
  await wrapper.get('#email').setValue('student@example.com');
  await wrapper.get('#sendCode').trigger('click'); await flushPromises();
  expect(wrapper.get('#authError').text()).toContain('HTTPS');
  expect(calls.some(url => url.endsWith('/verification-code'))).toBe(false);
  expect(document.querySelector('script[src*="vaptcha"]')).toBeNull();
});
