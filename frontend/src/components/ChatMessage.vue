<script setup>
import { computed, inject } from 'vue';
import { chatKey, displayContent } from '../composables/useChat.js';
import MarkdownContent from './MarkdownContent.vue';
import Icon from './Icon.vue';
const props = defineProps({ message: { type: Object, required: true } });
const chat = inject(chatKey);
const status = computed(() => props.message.notice || ({ interrupted: '回答未完整显示，可继续接收或重新打开会话查看保存内容。', failed: '这次回答未完成，可调整问题后重新提问。' }[props.message.status] || ''));
</script>

<template>
  <article v-if="['user', 'assistant'].includes(message.role)" class="message" :class="message.role" :data-role="message.role">
    <div v-if="message.role === 'user'" class="user-bubble">{{ displayContent(message) }}</div>
    <template v-else>
      <div class="assistant-heading"><img src="/icon.svg" alt=""><span>启学</span></div>
      <MarkdownContent v-if="message.content" :content="message.content" @copy="chat.copy" />
      <div v-else class="message-body"><span v-if="message.status === 'streaming'" class="typing">正在思考 ···</span></div>
      <div v-if="message.content && message.status !== 'streaming'" class="message-actions">
        <button class="icon-btn" title="复制回答" aria-label="复制回答" @click="chat.copy(message.content)"><Icon name="copy" /></button>
        <template v-if="message.id && !message.local">
          <button v-for="[rating, label] in [['up', '有帮助'], ['down', '帮助不大']]" :key="rating" class="icon-btn" :class="{ rated: message.feedback === rating }" :title="label" :aria-label="label" :aria-pressed="message.feedback === rating" :disabled="message.feedbackBusy" @click="chat.feedback(message, rating)"><Icon :name="rating" /></button>
        </template>
      </div>
      <p v-if="status" class="message-status">{{ status }}</p>
    </template>
  </article>
</template>
