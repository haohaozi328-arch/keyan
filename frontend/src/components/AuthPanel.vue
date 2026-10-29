<script setup>
import { computed, inject, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { chatKey } from '../composables/useChat.js';
import Icon from './Icon.vue';
const chat = inject(chatKey), { state } = chat;
const form = reactive({ email: '', password: '', nickname: '', code: '' });
const emailInput = ref(null), now = ref(Date.now());
const register = computed(() => state.authMode === 'register');
const cooldown = computed(() => Math.max(0, Math.ceil((state.codeUntil - now.value) / 1000)));
let timer, captcha = null;
onMounted(() => { timer = setInterval(() => { now.value = Date.now(); }, 1000); emailInput.value?.focus(); });
onUnmounted(() => { clearInterval(timer); captcha?.destroy?.(); });
watch(() => state.dialog, name => { if (name !== 'auth') { form.password = ''; form.code = ''; } });
function mode(value) { if (state.authSubmitting || state.codeSending) return; state.authMode = value; state.authError = ''; }
function close() { if (state.authSubmitting || state.codeSending) return; state.authError = ''; state.dialog = ''; }
// SDK 在 PC 上默认用 window.open 弹一个独立验证窗口，再用 postMessage 把 boot 递进去。
// Edge 里这条通道不成立：窗口能打开、标题也对，但内容一直是白屏（停在“验证准备中”），
// 不报错也不给入口 —— 就是那个“二级窗口没有验证码”的现象。
// 把内嵌分支固定为开启，验证面片直接展开在当前页面（SDK 自己挂的 #vaptcha_iframe_overlay）。
// 这不是官方 API，SDK 升级后要复查；能改平台配置的话，优先在 VAPTCHA 控制台把验证单元设成内嵌形态。
function forceInlineVerify(instance) {
  const proto = window.VaptchaCore?.prototype;
  if (proto && typeof proto.shouldUseIframe === 'function' && !proto.__inlineForced) {
    proto.shouldUseIframe = () => true;
    proto.__inlineForced = true;
  }
  try { if (instance && typeof instance.shouldUseIframe === 'function') instance.shouldUseIframe = () => true; } catch { /* 只读属性就跳过 */ }
}
// 这里是主区域里的一级页面，不是 <dialog> 弹窗：人机验证的浮层由 SDK 挂在 body 下，
// 靠 z-index 就能盖在页面内容之上。若改成 showModal() 的弹窗，弹窗会进入浏览器
// top layer，层级高于任何 z-index，验证面板会被整块遮住（表现为“没有验证码”）。
async function ticket() {
  if (location.protocol !== 'https:') throw new Error('此后端启用了人机验证。注册需要使用HTTPS页面；已有账号可直接登录。设置方法见使用说明。');
  if (!state.config?.captchaVid) throw new Error('人机验证尚未配置，请联系项目负责人。');
  if (!window.vaptcha) await new Promise((resolve, reject) => {
    const script = document.createElement('script'); script.src = 'https://c4.vaptcha.com/src/v4.js';
    script.onload = resolve; script.onerror = () => { script.remove(); reject(new Error('人机验证暂时无法加载，请检查网络。')); }; document.head.append(script);
  });
  if (!captcha) captcha = await window.vaptcha({ vid: state.config.captchaVid, container: '#vaptcha-container', lang: 'zh-CN' });
  forceInlineVerify(captcha);
  const result = await captcha.validate();
  if (!result?.token || !result?.knock) throw new Error('请完成人机验证后重新获取验证码。');
  return Object.fromEntries(['token', 'knock', 'dfu', 'ip'].map(k => [k, result[k] || '']));
}
function sendCode() { if (emailInput.value.reportValidity()) chat.sendCode(form.email, ticket); }
async function submit() { if (await chat.login(form)) Object.assign(form, { email: '', password: '', nickname: '', code: '' }); }
</script>

<template>
  <section id="authPage" class="auth-page" aria-labelledby="authTitle">
    <div class="auth-card">
      <button id="closeAuth" class="text-btn auth-back" @click="close">← 返回对话</button>
      <img class="modal-logo" src="/icon.svg" alt=""><h2 id="authTitle">欢迎来到启学</h2><p class="modal-sub">保存每次探索，接着上一次的思路。</p>
      <div class="auth-tabs" role="tablist" aria-label="登录或注册"><button id="loginTab" role="tab" :aria-selected="!register" :class="{ selected: !register }" @click="mode('login')">登录</button><button id="registerTab" role="tab" :aria-selected="register" :class="{ selected: register }" @click="mode('register')">注册</button></div>
      <form id="authForm" @submit.prevent="submit">
        <label for="email">邮箱</label><input id="email" ref="emailInput" v-model="form.email" type="email" required autocomplete="username" placeholder="输入你的邮箱" :disabled="state.authSubmitting">
        <label for="password">密码</label><input id="password" v-model="form.password" type="password" required :minlength="register ? 8 : 1" maxlength="128" :autocomplete="register ? 'new-password' : 'current-password'" :placeholder="register ? '设置8—128位密码' : '输入密码'" :disabled="state.authSubmitting">
        <div id="registration" v-show="register"><label for="nickname">昵称 <span class="optional">选填</span></label><input id="nickname" v-model="form.nickname" maxlength="64" autocomplete="nickname" placeholder="怎么称呼你"><label for="emailCode">邮箱验证码</label><div class="code-row"><input id="emailCode" v-model="form.code" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" :required="register" autocomplete="one-time-code" placeholder="6 位验证码"><button id="sendCode" type="button" class="secondary-btn" :disabled="state.codeSending || cooldown > 0 || state.authSubmitting" @click="sendCode">{{ state.codeSending ? '正在发送…' : cooldown > 0 ? cooldown + '秒后重发' : '获取验证码' }}</button></div><div id="vaptcha-container" /><p class="form-note">注册即创建本系统账号。问题和回答将保存到后端，用于提供历史会话。</p></div>
        <p id="authError" class="form-error" role="alert">{{ state.authError }}</p><button id="authSubmit" type="submit" class="primary-btn" :disabled="state.authSubmitting">{{ state.authSubmitting ? '请稍候…' : register ? '创建账号' : '登录' }}</button>
      </form>
    </div>
  </section>
</template>
