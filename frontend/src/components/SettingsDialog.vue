<script setup>
import { inject } from 'vue';
import { chatKey } from '../composables/useChat.js';
import BaseDialog from './BaseDialog.vue';
import Icon from './Icon.vue';
const chat = inject(chatKey), { state, loggedIn } = chat;
</script>

<template>
  <BaseDialog id="settingsDialog" class="settings-modal" :open="state.dialog === 'settings'" labelledby="settingsTitle" @close="state.dialog = ''">
    <button class="icon-btn close-dialog" aria-label="关闭设置" @click="state.dialog = ''"><Icon name="x" /></button><h2 id="settingsTitle">连接与设置</h2>
    <div class="setting-row"><span>后端连接</span><strong id="backendState">{{ state.backendState }}</strong></div><div class="setting-row"><span>模型配置</span><strong id="modelState">{{ state.modelState }}</strong></div><p class="form-note">登录状态保留在本次浏览器标签页中；聊天历史保存在后端。关闭标签页后需要重新登录。</p>
    <button id="checkConnection" class="secondary-btn full-width" @click="chat.checkConnection">重新检查连接</button><button v-if="loggedIn" id="logoutBtn" class="danger-btn full-width" @click="chat.logout">退出登录</button>
  </BaseDialog>
</template>
