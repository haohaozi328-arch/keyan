<script setup>
import { inject, nextTick, ref, watch } from 'vue';
import { chatKey } from '../composables/useChat.js';
import Icon from './Icon.vue';
const chat = inject(chatKey);
const { state, sendDisabled } = chat;
const textarea = ref(null);
function resize() { if (!textarea.value) return; textarea.value.style.height = 'auto'; textarea.value.style.height = Math.min(textarea.value.scrollHeight, 180) + 'px'; }
watch(() => state.question, () => nextTick(resize));
function keydown(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!state.busy) chat.submitQuestion(); }
}
defineExpose({ focus: () => textarea.value?.focus() });
</script>

<template>
  <div class="composer-area"><div class="composer-inner">
    <div v-if="state.notice" id="statusBanner" class="status-banner" role="status"><span id="statusText">{{ state.notice }}</span><button v-if="state.resumable" id="resumeBtn" class="text-btn" :disabled="state.busy || state.loading" @click="chat.resume">继续接收</button></div>
    <form id="composer" class="composer" @submit.prevent="state.busy ? chat.stop() : chat.submitQuestion()">
      <label class="sr-only" for="question">输入你的问题</label><textarea id="question" ref="textarea" v-model="state.question" rows="2" maxlength="7800" :disabled="state.loading" placeholder="把问题、代码，或你的思路写在这里…" @keydown="keydown" />
      <div class="composer-bottom"><div class="tools"><button id="hintMode" type="button" class="mode-chip" :class="{ active: state.hint }" :aria-pressed="state.hint" @click="state.hint = !state.hint"><span class="mini-spark">✧</span>逐步提示</button><span class="input-hint">Enter 发送 · Shift Enter 换行</span></div><button id="sendBtn" type="submit" class="send-btn" :class="{ stopping: state.busy }" :disabled="sendDisabled" :aria-label="state.busy ? '停止接收回答' : '发送消息'" :title="state.busy ? '停止接收回答' : '发送消息'"><Icon name="send" /></button></div>
    </form><p class="disclaimer">AI 回答仅供学习参考，重要结论请结合教材、计算或实验核验。</p>
  </div></div>
</template>
