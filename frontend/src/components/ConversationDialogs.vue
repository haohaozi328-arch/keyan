<script setup>
import { inject } from 'vue';
import { chatKey } from '../composables/useChat.js';
import BaseDialog from './BaseDialog.vue';
const chat = inject(chatKey), { state } = chat;
</script>

<template>
  <BaseDialog id="editDialog" :open="state.dialog === 'edit'" labelledby="editTitle" @close="state.dialog = ''">
    <h2 id="editTitle">修改会话标题</h2><form id="editForm" @submit.prevent="chat.rename"><label for="editInput">标题</label><input id="editInput" v-model="state.editTitle" required maxlength="100"><div class="dialog-actions"><button type="button" class="secondary-btn" @click="state.dialog = ''">取消</button><button type="submit" class="primary-btn" :disabled="state.mutationBusy">保存</button></div></form>
  </BaseDialog>
  <BaseDialog id="deleteDialog" :open="state.dialog === 'delete'" labelledby="deleteTitle" @close="state.dialog = ''">
    <h2 id="deleteTitle">删除这段会话？</h2><p class="modal-sub">会话将从列表中移除。后端仍可能保留相关记录。</p><div class="dialog-actions"><button class="secondary-btn" @click="state.dialog = ''">取消</button><button id="confirmDelete" class="danger-btn" :disabled="state.mutationBusy" @click="chat.remove">删除会话</button></div>
  </BaseDialog>
</template>
