<script setup>
import { nextTick, onMounted, onUnmounted, provide, ref, watch } from 'vue';
import { useChat, chatKey } from './composables/useChat.js';
import Icon from './components/Icon.vue';
import IconDefinitions from './components/IconDefinitions.vue';
import ChatSidebar from './components/ChatSidebar.vue';
import WelcomePanel from './components/WelcomePanel.vue';
import ChatMessage from './components/ChatMessage.vue';
import ChatComposer from './components/ChatComposer.vue';
import AuthDialog from './components/AuthDialog.vue';
import SettingsDialog from './components/SettingsDialog.vue';
import ConversationDialogs from './components/ConversationDialogs.vue';
const props = defineProps({ clientOptions: { type: Object, default: () => ({}) } });
const chat = useChat(props.clientOptions), { state, welcome, sidebarVisible } = chat;
provide(chatKey, chat);
const viewport = ref(null), composer = ref(null), menu = ref(null);
const media = window.matchMedia('(max-width:900px)');
let initialization;
function resize() { state.mobile = media.matches; chat.closeSidebar(); }
function keydown(e) {
  if (e.key === 'Escape' && state.sidebarOpen) { chat.closeSidebar(); nextTick(() => menu.value?.focus()); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !state.dialog) { e.preventDefault(); chat.newChat(); composer.value?.focus(); }
}
function scrollBottom(force = false) {
  const v = viewport.value;
  if (v && (force || v.scrollHeight - v.scrollTop - v.clientHeight < 180)) v.scrollTop = v.scrollHeight;
}
watch(() => state.messages.at(-1)?.content, () => {
  const v = viewport.value, nearBottom = v && v.scrollHeight - v.scrollTop - v.clientHeight < 180;
  if (nearBottom) nextTick(() => scrollBottom(true));
});
watch(() => state.scrollVersion, () => nextTick(() => scrollBottom(true)));
watch(() => state.sidebarClosed, closed => document.body.classList.toggle('sidebar-closed', closed));
async function older() {
  const v = viewport.value, height = v.scrollHeight, top = v.scrollTop;
  await chat.openConversation({ id: state.current, title: state.title }, true);
  await nextTick(); v.scrollTop = top + v.scrollHeight - height;
}
function prompt(text) { state.question = text; composer.value?.focus(); }
onMounted(() => { resize(); media.addEventListener('change', resize); document.addEventListener('keydown', keydown); initialization = chat.init(); });
onUnmounted(() => { chat.dispose(); media.removeEventListener('change', resize); document.removeEventListener('keydown', keydown); document.body.classList.remove('sidebar-closed'); });
defineExpose({ chat, ready: () => initialization });
</script>

<template>
  <IconDefinitions /><ChatSidebar />
  <main class="main" :inert="state.mobile && state.sidebarOpen">
    <header class="topbar"><div class="topbar-left"><button id="menuBtn" ref="menu" class="icon-btn" aria-label="展开侧栏" :aria-expanded="sidebarVisible" @click="chat.toggleSidebar"><Icon name="menu" /></button><span class="page-brand">启学</span><span class="topbar-divider" /><span id="conversationTitle">{{ state.title || '学习，从一个好问题开始' }}</span></div><div class="topbar-right"><span id="connection" class="connection" :class="state.connection"><i /><span>{{ state.connectionText }}</span></span><button id="exportBtn" class="icon-btn" title="导出当前对话" aria-label="导出当前对话" :disabled="!state.messages.length" @click="chat.downloadChat"><Icon name="download" /></button></div></header>
    <section id="viewport" ref="viewport" class="viewport" aria-label="聊天内容">
      <WelcomePanel v-if="welcome" @prompt="prompt" />
      <div v-else id="thread" class="thread"><button v-if="state.before" id="olderMessages" class="text-btn older" :disabled="state.busy || state.loading" @click="older">查看更早的消息</button><div id="messages" aria-live="polite" aria-relevant="additions"><ChatMessage v-for="(m, index) in state.messages" :key="m.id || 'local-' + index" :message="m" /></div></div>
    </section><ChatComposer ref="composer" />
  </main>
  <AuthDialog /><SettingsDialog /><ConversationDialogs />
  <div v-if="state.toast" id="toast" class="toast" role="status">{{ state.toast }}</div>
</template>
