import { requestJSON } from './api.js';

const micBtn = document.getElementById('mic');
const speakBtn = document.getElementById('speak-toggle');
const voiceSelect = document.getElementById('voice-select');
const input = document.getElementById('input');
const statusEl = document.getElementById('voice-status');

// --- 音色选择（写死常用中文音色，存 localStorage）---

const VOICES = [
  { id: 'zh-CN-XiaoxiaoNeural', label: '晓晓 · 温柔' },
  { id: 'zh-CN-XiaoyiNeural', label: '晓伊 · 活泼' },
  { id: 'zh-CN-YunxiNeural', label: '云希 · 清朗' },
  { id: 'zh-CN-YunjianNeural', label: '云健 · 沉稳' },
  { id: 'zh-CN-XiaochenNeural', label: '晓辰 · 知性' },
  { id: 'zh-CN-YunyangNeural', label: '云扬 · 播报' },
];

const VOICE_KEY_PREFIX = 'niannian.voice.';
const LEGACY_VOICE_KEY = 'niannian.voice';
const AUTO_READ_KEY = 'niannian.auto_read';
const DEFAULT_VOICE = VOICES[0].id;

for (const voice of VOICES) {
  const option = document.createElement('option');
  option.value = voice.id;
  option.textContent = voice.label;
  voiceSelect.appendChild(option);
}

function currentPersonaId() {
  try {
    const sessions = JSON.parse(localStorage.getItem('niannian.sessions') || '[]');
    const threadId = localStorage.getItem('niannian.thread_id');
    const session = sessions.find((item) => item.id === threadId);
    return session?.persona_id || 1;
  } catch {
    return 1;
  }
}

function voiceKey(personaId) {
  return `${VOICE_KEY_PREFIX}${personaId}`;
}

function loadVoiceForPersona(personaId = currentPersonaId()) {
  // 每个角色独立音色；没有单独设置过时用默认，并兼容旧的全局音色
  const saved =
    localStorage.getItem(voiceKey(personaId)) || localStorage.getItem(LEGACY_VOICE_KEY);
  voiceSelect.value = VOICES.some((voice) => voice.id === saved) ? saved : DEFAULT_VOICE;
}

loadVoiceForPersona();

voiceSelect.addEventListener('change', () => {
  localStorage.setItem(voiceKey(currentPersonaId()), voiceSelect.value);
});

// 切换会话/角色时，音色跟着当前角色走
document.addEventListener('niannian:session-changed', (event) => {
  const personaId = event.detail?.personaId;
  if (personaId) loadVoiceForPersona(personaId);
  else loadVoiceForPersona();
});

// --- 语音输入（Web Speech API，仅 Edge/Chrome 支持）---

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
// 非 https/localhost 时浏览器一定拒绝麦克风，直接隐藏按钮
const canRecognize = Boolean(Recognition) && window.isSecureContext;
let recognition = null;

if (!canRecognize) {
  micBtn.hidden = true;
} else {
  recognition = new Recognition();
  recognition.lang = 'zh-CN';
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;

  recognition.onresult = (event) => {
    const text = event.results?.[0]?.[0]?.transcript || '';
    if (!text) return;
    input.value = text;
    // 触发 chat.js 的自动高度与输入状态
    input.dispatchEvent(new Event('input'));
    input.focus();
    statusEl.textContent = '已识别，可发送';
  };

  recognition.onerror = (event) => {
    micBtn.classList.remove('active');
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      statusEl.textContent = '请允许麦克风权限后重试';
    } else {
      statusEl.textContent = '语音识别失败，请重试';
    }
  };

  recognition.onend = () => {
    micBtn.classList.remove('active');
    if (statusEl.textContent === '正在听…') statusEl.textContent = '';
  };

  micBtn.addEventListener('click', () => {
    if (micBtn.classList.contains('active')) {
      recognition.stop();
      return;
    }
    // 开始录音前停掉朗读，避免把喇叭里的 AI 声音录进去
    stopPlayback();
    try {
      recognition.start();
      micBtn.classList.add('active');
      statusEl.textContent = '正在听…';
    } catch {
      statusEl.textContent = '无法启动语音输入';
    }
  });
}

// --- 朗读：按一次进入自动模式，再按一次退出 ---
//
// 不用 <audio> 元素：浏览器对它的自动播放限制会在用户手势过期后
// 把后续 play() 挂起。改用 AudioContext，在手势内 resume 一次后，
// 后续 BufferSource 可以稳定播放。

let audioContext = null;
let currentSource = null;
let speaking = false;
// 每次朗读请求一个令牌；旧请求/旧播放都会被新令牌作废
let playToken = 0;
let speakTimer = null;
let autoRead = localStorage.getItem(AUTO_READ_KEY) === '1';

function syncToggle() {
  speakBtn.classList.toggle('active', autoRead);
  speakBtn.setAttribute('aria-pressed', String(autoRead));
  speakBtn.textContent = autoRead ? '语音回复中' : '语音回复';
}

function resetVoiceUi() {
  speaking = false;
  statusEl.textContent = '';
}

function unlockAudio() {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  if (!audioContext) audioContext = new Ctx();
  if (audioContext.state === 'suspended') {
    audioContext.resume().catch(() => {});
  }
}

function stopPlayback() {
  // 让所有正在请求/播放的旧朗读失效
  playToken += 1;
  if (currentSource) {
    try {
      currentSource.stop();
    } catch {
      // 已经结束的 source 再 stop 会抛错，忽略
    }
    currentSource = null;
  }
  if ('speechSynthesis' in window) {
    speechSynthesis.cancel();
  }
  resetVoiceUi();
}

function pickBubble() {
  const bubbles = [...document.querySelectorAll('.msg.ai .bubble')];
  for (let index = bubbles.length - 1; index >= 0; index -= 1) {
    const bubble = bubbles[index];
    // 主动关怀气泡不参与朗读，避免读到通知而不是回复
    if (bubble.classList.contains('proactive')) continue;
    // 失败轮次的气泡也不读（占位文字、半截回复）
    if (bubble.dataset.failed === '1') continue;
    return bubble;
  }
  return null;
}

function fallbackSpeak(text, token) {
  if (!('speechSynthesis' in window)) {
    if (token === playToken) resetVoiceUi();
    statusEl.textContent = '当前浏览器不支持语音播放';
    return;
  }
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = 'zh-CN';
  utterance.onend = () => {
    if (token === playToken) resetVoiceUi();
  };
  utterance.onerror = () => {
    if (token === playToken) resetVoiceUi();
  };
  speechSynthesis.speak(utterance);
}

async function playWithAudioContext(blob, token) {
  const arrayBuffer = await blob.arrayBuffer();
  if (token !== playToken) return;

  const buffer = await audioContext.decodeAudioData(arrayBuffer);
  if (token !== playToken) return;

  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(audioContext.destination);
  currentSource = source;
  source.onended = () => {
    if (token === playToken) {
      currentSource = null;
      resetVoiceUi();
    }
  };
  source.start();
}

async function speak(bubble) {
  if (!bubble) return;
  if (bubble.classList.contains('typing')) {
    statusEl.textContent = '回复还没结束，稍后再试';
    return;
  }

  const text = (bubble.textContent || '').trim();
  if (!text) return;
  // 朗读前停止录音，避免把自己的声音录进去
  if (recognition) {
    try {
      recognition.stop();
    } catch {
      // 没在录音时 stop 会抛错，忽略
    }
  }
  // 标记已处理，避免事件和观察器重复朗读同一条
  bubble.dataset.spoken = '1';

  // 新的一条优先：停掉还在播的旧语音
  stopPlayback();
  const token = ++playToken;
  speaking = true;
  statusEl.textContent = '正在生成语音…';

  try {
    const response = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        voice: voiceSelect.value,
        emotion: bubble.dataset.emotion || undefined,
      }),
    });
    if (!response.ok) throw new Error(`tts ${response.status}`);

    const blob = await response.blob();
    if (token !== playToken) return;

    if (!audioContext) unlockAudio();
    if (audioContext) {
      if (audioContext.state === 'suspended' || audioContext.state === 'interrupted') {
        try {
          await audioContext.resume();
        } catch {
          // 恢复失败走下面的降级
        }
      }
      if (audioContext.state === 'running') {
        await playWithAudioContext(blob, token);
        return;
      }
    }

    // AudioContext 不可用或被挂起：退回浏览器朗读
    fallbackSpeak(text, token);
  } catch {
    if (token !== playToken) return;
    // 允许同一气泡在失败后重试
    delete bubble.dataset.spoken;
    fallbackSpeak(text, token);
  }
}

speakBtn.addEventListener('click', () => {
  autoRead = !autoRead;
  localStorage.setItem(AUTO_READ_KEY, autoRead ? '1' : '0');
  syncToggle();

  if (autoRead) {
    // 必须在用户手势内解锁，之后自动播放才稳定
    unlockAudio();
    const bubble = pickBubble();
    if (bubble) {
      speakTimer = setTimeout(() => {
        if (autoRead) speak(bubble);
      }, 60);
    } else {
      statusEl.textContent = '还没有可以朗读的回复';
    }
  } else {
    clearTimeout(speakTimer);
    stopPlayback();
  }
});

// chat.js 在每轮回复结束时派发；自动朗读模式下开读
document.addEventListener('niannian:reply-done', (event) => {
  if (!autoRead) return;
  const bubble = event.detail?.bubble;
  // 观察器可能已经先读过这条，避免重复
  if (bubble && bubble.dataset.spoken !== '1') speak(bubble);
});

syncToggle();

// 每次点击或按键（发送消息、切标签等）都在用户手势内解锁一次：
// 浏览器可能在两次播放之间把 AudioContext 挂起，回复到来时已经没有手势了
document.addEventListener('pointerdown', () => unlockAudio());
document.addEventListener('keydown', () => unlockAudio());

// 设置面板保存后，把语音相关开关同步到页面
function applySettings(settings) {
  if (!settings) return;

  if (settings.asr_enabled === 0) {
    micBtn.hidden = true;
  } else if (canRecognize) {
    micBtn.hidden = false;
  }

  const settingAuto = settings.auto_read === 1;
  if (settingAuto !== autoRead) {
    autoRead = settingAuto;
    localStorage.setItem(AUTO_READ_KEY, autoRead ? '1' : '0');
    syncToggle();
    if (!autoRead) stopPlayback();
  }

  // 音色按角色独立，设置面板里的 voice_name 只作为服务端默认，不覆盖角色选择
}

async function loadVoiceSettings() {
  try {
    const data = await requestJSON('/api/settings');
    applySettings(data.settings);
  } catch {
    // 读取失败时沿用本地状态
  }
}

document.addEventListener('niannian:settings-changed', (event) => {
  applySettings(event.detail?.settings);
});

loadVoiceSettings();

// 兜底触发：即使 chat 脚本没派发事件，也监听气泡的 typing 类被移除
const messagesEl = document.getElementById('messages');
new MutationObserver(() => {
  if (!autoRead) return;
  const bubble = pickBubble();
  if (!bubble || bubble.classList.contains('typing')) return;
  if (bubble.dataset.spoken === '1') return;
  speak(bubble);
}).observe(messagesEl, { subtree: true, attributes: true, attributeFilter: ['class'] });

// 便于在控制台确认加载的是新版脚本
document.body.dataset.voiceBuild = 'web-audio';
