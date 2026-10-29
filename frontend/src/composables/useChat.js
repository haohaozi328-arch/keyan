import { computed, reactive, shallowRef } from 'vue';
import { ApiClient, ApiError, readSSE } from '../../api-client.mjs';

export const chatKey = Symbol('chat');
const hintPrefix = '【逐步提示学习方式】请先参考我的尝试，只给一小步提示，等我反馈后再深入；需要时提醒我用教材、计算、运行或仿真核验。\n\n';
export const displayContent = m => m.role === 'user' && m.content.startsWith(hintPrefix) ? m.content.slice(hintPrefix.length) : m.content;

export function useChat(options = {}) {
  const auth = shallowRef(null);
  const state = reactive({
    current: null, title: '', messages: [], conversations: [], cursor: null, before: null,
    busy: false, loading: false, historyLoading: false, hint: true, pending: null, question: '',
    notice: '', resumable: false, toast: '', dialog: '', authMode: 'login', authError: '',
    authSubmitting: false, codeSending: false, codeUntil: 0, config: null,
    editing: null, editTitle: '', deleting: null, mutationBusy: false,
    mobile: false, sidebarOpen: false, sidebarClosed: false,
    connection: 'checking', connectionText: '正在连接', backendState: '检查中', modelState: '检查中',
    scrollVersion: 0,
  });
  let controller = null, toastTimer = null, viewVersion = 0, disposed = false;
  const fetcher = options.fetcher || globalThis.fetch.bind(globalThis);
  const api = new ApiClient({ fetcher, storage: options.storage ?? globalThis.sessionStorage,
    onChange: value => { auth.value = value; },
    onExpire: () => { resetSession(); toast('登录已过期，请重新登录。'); openAuth(); },
  });
  auth.value = api.auth;
  const loggedIn = computed(() => Boolean(auth.value?.token?.access_token));
  const welcome = computed(() => !state.current && !state.messages.length);
  const sidebarVisible = computed(() => state.mobile ? state.sidebarOpen : !state.sidebarClosed);
  const sendDisabled = computed(() => state.loading || (!state.busy && (!state.question.trim() || Boolean(state.pending))));
  function toast(text) { if (disposed) return; state.toast = text; clearTimeout(toastTimer); toastTimer = setTimeout(() => { state.toast = ''; }, 4000); }
  function banner(text = '', resumable = false) { state.notice = text; state.resumable = resumable; }
  function closeSidebar() { state.sidebarOpen = false; }
  function toggleSidebar() { if (state.mobile) state.sidebarOpen = !state.sidebarOpen; else state.sidebarClosed = !state.sidebarClosed; }
  function openDialog(name) { closeSidebar(); state.dialog = name; }
  function openAuth() { openDialog('auth'); }
  function resetSession() {
    viewVersion++; controller?.abort(); state.pending = null; state.current = null; state.title = '';
    state.messages = []; state.conversations = []; state.cursor = null; state.before = null;
    state.loading = false; state.historyLoading = false; banner();
  }
  function newChat() {
    if (state.busy || state.loading) return toast('请先停止接收，或等当前操作完成。');
    viewVersion++; state.current = null; state.title = ''; state.messages = []; state.before = null; state.pending = null;
    banner(); closeSidebar();
  }
  async function loadHistory(more = false) {
    if (!api.loggedIn) return;
    const session = api.sessionVersion;
    state.historyLoading = true;
    try {
      const r = await api.json('/conversations?limit=20' + (more && state.cursor ? '&cursor=' + encodeURIComponent(state.cursor) : ''));
      if (disposed || session !== api.sessionVersion) return;
      state.conversations = [...new Map([...(more ? state.conversations : []), ...r.items].map(c => [c.id, c])).values()];
      state.cursor = r.next_cursor;
    } catch (e) { if (session === api.sessionVersion) toast(e.message); }
    finally { if (session === api.sessionVersion) state.historyLoading = false; }
  }
  async function openConversation(c, older = false) {
    if (state.busy || state.loading) return toast('请先停止接收，或等待消息加载完成。');
    const view = ++viewVersion;
    state.loading = true;
    try {
      const r = await api.json('/conversations/' + encodeURIComponent(c.id) + '/messages?limit=50' + (older && state.before ? '&before_seq=' + state.before : ''));
      if (disposed || view !== viewVersion) return;
      state.current = c.id; state.title = c.title; state.before = r.next_before_seq;
      state.messages = older ? [...r.items, ...state.messages] : r.items; state.pending = null;
      const generating = state.messages.findLast(m => m.role === 'assistant' && m.status === 'streaming');
      if (generating) { state.pending = { message: generating, messageId: generating.id, conversationId: c.id, lastId: null }; banner('这条回答尚在生成，可继续接收已生成内容。', true); }
      else banner();
      closeSidebar();
      if (!older) state.scrollVersion++;
    } catch (e) { if (view === viewVersion) toast(e.message); }
    finally { if (view === viewVersion) state.loading = false; }
  }
  function parseEvent(frame) { try { return JSON.parse(frame.data); } catch { throw new ApiError('流式数据格式有误，请重新读取会话。'); } }
  async function receiveTurn(pending, initialMessage) {
    const active = new AbortController(), view = viewVersion;
    controller = active; state.busy = true; banner();
    let terminal = false;
    const m = pending.message;
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const resuming = Boolean(pending.messageId);
          const response = await api.request(resuming ? '/chat/stream/' + encodeURIComponent(pending.messageId) : '/chat/stream', {
            method: resuming ? 'GET' : 'POST', body: resuming ? undefined : { message: initialMessage, ...(state.current ? { conversation_id: state.current } : {}) },
            headers: resuming && pending.lastId ? { 'Last-Event-ID': pending.lastId } : {}, signal: active.signal,
          });
          if (resuming && !pending.lastId) m.content = '';
          await readSSE(response, frame => {
            if (view !== viewVersion || disposed) throw new DOMException('Aborted', 'AbortError');
            if (frame.id && frame.id === pending.lastId) return;
            if (frame.event === 'start' || frame.event === 'meta') {
              const d = parseEvent(frame); pending.messageId = d.message_id; pending.conversationId = d.conversation_id;
              state.current = d.conversation_id; m.id = d.message_id; m.local = false;
            } else if (frame.event === 'delta') m.content += parseEvent(frame).delta;
            else if (frame.event === 'done') { m.status = 'complete'; terminal = true; }
            else if (frame.event === 'error') { const d = parseEvent(frame); terminal = true; throw new ApiError(d.message, 0, d.code); }
            else if (frame.event === 'shutdown') throw new ApiError('服务正在重启，可稍后继续接收。');
            if (frame.id) pending.lastId = frame.id;
            return !terminal;
          }, { signal: active.signal });
          if (!terminal) throw new ApiError('回答连接中断。');
          break;
        } catch (e) {
          if (e.name === 'AbortError' || active.signal.aborted) throw e;
          if (e.status === 410) { m.status = 'interrupted'; m.notice = '续传窗口已过期，请重新打开此会话查看后端保存的回答。'; terminal = true; throw e; }
          if (pending.messageId && !e.code && !e.status && attempt < 2) { banner('连接中断，正在接回这一条回答…'); continue; }
          throw e;
        }
      }
      if (terminal) { m.notice = ''; state.pending = null; banner(); }
    } catch (e) {
      if (view !== viewVersion || disposed) return;
      if (e.status === 410) { state.pending = null; m.status = 'interrupted'; banner(m.notice); }
      else if (pending.messageId && !terminal && api.loggedIn) {
        state.pending = pending; m.status = 'interrupted'; m.notice = '';
        banner(active.signal.aborted ? '已停止接收，服务端可能继续短暂生成。可继续接收，或新建对话。' : e.message, true);
      } else {
        state.pending = null; m.status = 'failed';
        m.notice = active.signal.aborted ? '已停止接收。请求可能已经提交，可刷新历史确认后再提问。' : e.message;
        banner(m.notice);
      }
    } finally {
      if (controller === active) { controller = null; state.busy = false; }
      if (!disposed && view === viewVersion && api.loggedIn) await loadHistory();
    }
  }
  async function submitQuestion(text = state.question) {
    if (state.busy || state.loading || !text.trim()) return;
    if (!api.loggedIn) return openAuth();
    if (state.pending) return toast('请先继续接收上一条回答，或开启新对话。');
    const question = text.trim(); if (question.length > 7800) return toast('请把问题缩短到7800字以内。');
    state.messages.push({ role: 'user', content: question, local: true }, { role: 'assistant', content: '', status: 'streaming', local: true });
    if (!state.title) state.title = question.slice(0, 28);
    const message = state.messages.at(-1);
    state.question = ''; state.scrollVersion++;
    await receiveTurn({ message, conversationId: state.current, lastId: null }, (state.hint ? hintPrefix : '') + question);
  }
  async function resume() {
    if (!state.pending || state.busy || state.loading) return;
    const pending = state.pending; pending.message.status = 'streaming';
    await receiveTurn(pending);
  }
  async function feedback(message, rating) {
    if (message.feedbackBusy) return;
    message.feedbackBusy = true;
    try { const r = await api.json('/messages/' + encodeURIComponent(message.id) + '/feedback', { method: 'POST', body: { rating: message.feedback === rating ? null : rating } }); message.feedback = r.rating; }
    catch (e) { toast(e.message); } finally { message.feedbackBusy = false; }
  }
  function beginRename(c) { if (state.busy || state.loading) return toast('请等待当前操作完成。'); state.editing = c.id; state.editTitle = c.title; openDialog('edit'); }
  function beginDelete(c) { if (state.busy || state.loading) return toast('请等待当前操作完成。'); state.deleting = c.id; openDialog('delete'); }
  async function rename() {
    const title = state.editTitle.trim(); if (!title || state.mutationBusy) return;
    state.mutationBusy = true;
    try {
      const r = await api.json('/conversations/' + encodeURIComponent(state.editing), { method: 'PATCH', body: { title } });
      if (state.current === r.id) state.title = r.title;
      state.dialog = ''; await loadHistory();
    } catch (e) { toast(e.message); } finally { state.mutationBusy = false; }
  }
  async function remove() {
    if (state.mutationBusy) return;
    state.mutationBusy = true;
    try {
      await api.json('/conversations/' + encodeURIComponent(state.deleting), { method: 'DELETE' });
      if (state.current === state.deleting) newChat();
      state.dialog = ''; await loadHistory(); toast('会话已移除');
    } catch (e) { toast(e.message); } finally { state.mutationBusy = false; }
  }
  async function login(form) {
    if (state.authSubmitting || state.busy) return;
    state.authSubmitting = true; state.authError = '';
    try {
      const body = { email: form.email.trim().toLowerCase(), password: form.password };
      if (state.authMode === 'register') { body.nickname = form.nickname.trim(); body.code = form.code; }
      const result = await api.json('/auth/' + state.authMode, { method: 'POST', body, authenticated: false });
      resetSession(); api.save(result); state.dialog = ''; banner(); await loadHistory(); toast('登录成功，可以开始提问了。');
      return true;
    } catch (e) { state.authError = e.message; return false; } finally { state.authSubmitting = false; }
  }
  async function sendCode(email, getTicket) {
    if (state.codeSending || state.codeUntil > Date.now()) return;
    state.codeSending = true; state.authError = '';
    try {
      if (!state.config) throw new Error('页面配置尚未加载，请稍后重试。');
      const ticket = state.config.captchaEnabled ? await getTicket() : {};
      const r = await api.json('/auth/verification-code', { method: 'POST', body: { email: email.trim().toLowerCase(), scene: 'register', ...ticket }, authenticated: false });
      state.codeUntil = Date.now() + r.resend_after * 1000; toast('验证码已发送，请查看邮箱。');
    } catch (e) { state.authError = e.message; } finally { state.codeSending = false; }
  }
  async function logout() {
    if (state.busy) return toast('请先停止接收回答。');
    try { await api.logout(); resetSession(); state.dialog = ''; toast('已退出登录'); } catch (e) { toast('退出未完成：' + e.message); }
  }
  async function copy(text) { try { await navigator.clipboard.writeText(text); toast('已复制'); } catch { toast('浏览器不允许自动复制，请选中文字复制。'); } }
  function downloadChat() {
    if (!state.messages.length) return;
    const text = '# ' + (state.title || '启学对话') + '\n\nAI内容需独立核验。\n\n' + state.messages.map(m => `## ${m.role === 'user' ? '我' : '启学'}\n\n${displayContent(m)}${m.status && m.status !== 'complete' ? '\n\n（回答状态：' + m.status + '）' : ''}`).join('\n\n');
    const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' })), a = document.createElement('a');
    a.href = url; a.download = '启学-对话记录.md'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function checkConnection() {
    state.connection = 'checking'; state.connectionText = '正在连接';
    const [ready, chat] = await Promise.allSettled([api.json('/health/ready', { authenticated: false }), api.json('/chat/status', { authenticated: false })]);
    if (disposed) return;
    const reachable = chat.status === 'fulfilled', healthy = ready.status === 'fulfilled';
    state.connection = healthy && reachable ? 'online' : 'offline';
    state.connectionText = healthy ? '后端已连接' : reachable ? '依赖待启动' : '后端未连接';
    state.backendState = healthy ? '服务与依赖就绪' : reachable ? '服务可达，依赖未就绪' : '未连接';
    state.modelState = reachable ? chat.value.configured ? chat.value.model : '未配置模型密钥' : '暂时无法检查';
  }
  async function init() {
    try { const r = await fetcher('/frontend-config.json'); if (!r.ok) throw new Error(); state.config = await r.json(); }
    catch { toast('页面配置读取失败，请刷新重试。'); }
    const connection = checkConnection();
    if (api.loggedIn) {
      try { const user = await api.json('/auth/me'); if (!disposed && api.auth) { api.save({ ...api.auth, user }, { rotating: true }); await loadHistory(); } }
      catch (e) { toast(e.message); }
    }
    await connection;
  }
  function dispose() { disposed = true; viewVersion++; controller?.abort(); clearTimeout(toastTimer); }
  return { state, auth, api, loggedIn, welcome, sidebarVisible, sendDisabled, toast, openDialog, openAuth, closeSidebar, toggleSidebar,
    newChat, loadHistory, openConversation, submitQuestion, resume, stop: () => controller?.abort(), feedback, beginRename, beginDelete,
    rename, remove, login, sendCode, logout, copy, downloadChat, checkConnection, init, dispose };
}
