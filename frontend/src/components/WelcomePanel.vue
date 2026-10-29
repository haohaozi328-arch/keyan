<script setup>
import Icon from './Icon.vue';
defineEmits(['prompt']);
const suggestions = [
  { icon: 'code', color: 'blue', title: '代码卡住了？', subtitle: '定位问题，理清调试思路', prompt: '我在调试一段代码。请先让我提供代码、报错和自己的尝试，再从最小复现开始，逐步给提示。' },
  { icon: 'book', color: 'violet', title: '换个角度理解', subtitle: '把抽象概念讲得更清楚', prompt: '请帮我理解一个课程概念。先问我具体概念和不理解的地方，再用直观例子逐步解释。' },
  { icon: 'circuit', color: 'green', title: '电路与信号分析', subtitle: '从已知条件走到推导过程', prompt: '请帮我分步分析电路或信号问题。先让我提供题目、连接关系、参数和自己的尝试，再引导分析，并说明适用条件。' },
  { icon: 'plan', color: 'amber', title: '安排下一步学习', subtitle: '把学习目标变成每日行动', prompt: '请帮我制定一份可执行的学习计划。先问我学习目标、截止时间、目前基础和每天可用时间。' },
];
</script>

<template>
  <div id="welcome" class="welcome">
    <div class="welcome-mark"><img src="/icon.svg" alt=""></div><p class="eyebrow">YOUR LEARNING COMPANION</p>
    <h1>一起把问题<span>想明白</span></h1><p class="welcome-sub">从一个提示开始，让每一步都有收获。</p>
    <div class="suggestions"><button v-for="s in suggestions" :key="s.icon" class="suggestion" @click="$emit('prompt', s.prompt)"><span class="suggestion-icon" :class="s.color"><Icon :name="s.icon" /></span><strong>{{ s.title }}</strong><small>{{ s.subtitle }}</small><span class="card-arrow">↗</span></button></div>
  </div>
</template>
