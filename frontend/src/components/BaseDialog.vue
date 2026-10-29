<script setup>
import { onBeforeUnmount, ref, watch } from 'vue';
const props = defineProps({ open: Boolean, labelledby: { type: String, required: true } });
const emit = defineEmits(['close']);
const dialog = ref(null);
watch(() => props.open, open => {
  if (!dialog.value) return;
  if (open && !dialog.value.open) dialog.value.showModal();
  else if (!open && dialog.value.open) dialog.value.close();
}, { immediate: true, flush: 'post' });
function backdrop(e) {
  if (e.target !== dialog.value) return;
  const r = dialog.value.getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) emit('close');
}
onBeforeUnmount(() => { if (dialog.value?.open) dialog.value.close(); });
</script>

<template><dialog ref="dialog" class="modal" :aria-labelledby="labelledby" @cancel.prevent="emit('close')" @click="backdrop"><slot /></dialog></template>
