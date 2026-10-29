<script setup>
import { ref, watchEffect } from 'vue';
import { renderMarkdown } from '../../markdown.mjs';
const props = defineProps({ content: { type: String, default: '' } });
const emit = defineEmits(['copy']);
const target = ref(null);
// This isolated renderer creates safe text nodes; raw model HTML is never executed.
watchEffect(() => { if (target.value) renderMarkdown(props.content, target.value, text => emit('copy', text)); }, { flush: 'post' });
</script>

<template><div ref="target" class="message-body" /></template>
