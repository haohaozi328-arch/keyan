<script setup>
import { inject } from 'vue';
import { chatKey } from '../composables/useChat.js';
import Icon from './Icon.vue';
const chat = inject(chatKey);
const { state, auth, loggedIn, sidebarVisible } = chat;
</script>

<template>
  <button v-if="state.mobile && state.sidebarOpen" id="scrim" class="scrim" aria-label="关闭侧栏" @click="chat.closeSidebar" />
  <aside id="sidebar" class="sidebar" :class="{ open: state.mobile && state.sidebarOpen, collapsed: !state.mobile && state.sidebarClosed }" :inert="!sidebarVisible" aria-label="会话侧栏">
    <a class="brand" href="/" aria-label="启学首页"><img src="/icon.svg" alt=""><span>启学<span class="brand-dot">.</span></span></a>
    <button id="newChat" class="new-chat" @click="chat.newChat"><Icon name="plus" />开启新对话<span>Ctrl K</span></button>
    <div class="history-head"><span>最近对话</span><button id="refreshHistory" class="icon-btn" aria-label="刷新会话列表" title="刷新会话列表" :disabled="state.historyLoading" @click="loggedIn ? chat.loadHistory() : chat.openAuth()">↻</button></div>
    <nav id="history" class="history" aria-label="历史对话">
      <p v-if="!loggedIn" class="history-empty">登录后，你的对话会保存在这里。</p>
      <p v-else-if="!state.conversations.length" class="history-empty">还没有对话。<br>从一个问题开始吧。</p>
      <div v-for="c in state.conversations" :key="c.id" class="history-item" :class="{ selected: c.id === state.current }">
        <button class="history-select" :title="c.title" @click="chat.openConversation(c)">{{ c.title || '新的对话' }}</button>
        <button class="icon-btn" :aria-label="'重命名：' + c.title" @click="chat.beginRename(c)"><Icon name="edit" /></button>
        <button class="icon-btn" :aria-label="'删除：' + c.title" @click="chat.beginDelete(c)"><Icon name="trash" /></button>
      </div>
    </nav>
    <button v-if="loggedIn && state.cursor" id="moreHistory" class="text-btn" :disabled="state.historyLoading" @click="chat.loadHistory(true)">加载更多会话</button>
    <div class="sidebar-bottom">
      <button id="settingsBtn" class="sidebar-link" @click="chat.openDialog('settings')"><Icon name="settings" />连接与设置</button>
      <button id="accountBtn" class="account" @click="loggedIn ? chat.openDialog('settings') : chat.openAuth()">
        <span class="avatar">{{ (auth?.user?.nickname || auth?.user?.email || '启').slice(0, 1).toUpperCase() }}</span>
        <span class="account-text"><strong>{{ auth?.user?.nickname || auth?.user?.email || '登录，保存你的思路' }}</strong><small>{{ loggedIn ? '已登录 · 继续你的学习' : '在不同对话间继续探索' }}</small></span><Icon name="chevron" />
      </button>
    </div>
  </aside>
</template>
