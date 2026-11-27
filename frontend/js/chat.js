import { chatStream } from './sse.js';

const STORAGE_KEY = 'niannian.thread_id';
const THREAD_ID_PATTERN = /^[A-Za-z0-9_-]{8,64}$/;
const messagesEl = document.getElementById('messages');
const form = document.getElementById('composer');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');
const emptyEl = document.getElementById('empty');

let sending = false;

function resolveThreadId() {
  let value = localStorage.getItem(STORAGE_KEY);
  // 旧值或手工写入的非法值会让后端每次返回 400，这里直接重新生成
  if (!value || !THREAD_ID_PATTERN.test(value)) {
    const random = crypto.randomUUID?.() || `${Date.now()}${Math.random().toString(16).slice(2)}`;
    value = random.replace(/-/g, '');
    localStorage.setItem(STORAGE_KEY, value);
  }
  return value;
}

const threadId = resolveThreadId();

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setStatus(text) {
  statusEl.textContent = text;
}

function addBubble(role, text = '') {
  emptyEl?.remove();

  const row = document.createElement('div');
  row.className = `msg ${role}`;

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  // textContent 而不是 innerHTML：用户输入和模型输出都可能包含 HTML
  bubble.textContent = text;

  row.appendChild(bubble);
  messagesEl.appendChild(row);
  scrollToBottom();
  return bubble;
}

function autoResize() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();

  const text = input.value.trim();
  if (!text || sending) return;

  sending = true;
  sendBtn.disabled = true;
  setStatus('正在想…');

  addBubble('me', text);
  input.value = '';
  autoResize();

  const bubble = addBubble('ai', '');
  bubble.classList.add('typing');

  try {
    await chatStream(
      { thread_id: threadId, message: text },
      {
        token(data) {
          bubble.textContent += data.text || '';
          scrollToBottom();
        },
        trace(data) {
          if (data.branch === 'safety') {
            bubble.classList.add('safety');
          }
          // 存到气泡上，朗读时带上情绪，后端才能调语速音调
          if (data.emotion) bubble.dataset.emotion = data.emotion;
        },
        error(data) {
          setStatus(data.message || '出错了');
          // 失败轮次不参与自动朗读（占位文字会被读出来）
          bubble.dataset.failed = '1';
          if (!bubble.textContent) bubble.textContent = '（没有收到回复）';
        },
        done() {
          bubble.classList.remove('typing');
          setStatus('');
          // 通知语音模块：这一轮回复完整了，自动朗读模式可以开读
          document.dispatchEvent(
            new CustomEvent('niannian:reply-done', { detail: { bubble } }),
          );
        },
      },
    );
  } catch (error) {
    setStatus(error.message || '网络错误');
    // 连接中断时可能只有半截回复，同样不朗读
    bubble.dataset.failed = '1';
    if (!bubble.textContent) bubble.textContent = '（连接失败）';
  } finally {
    bubble.classList.remove('typing');
    sending = false;
    sendBtn.disabled = false;
    input.focus();
  }
});

input.addEventListener('keydown', (event) => {
  // 中文输入法选词时按 Enter 不应触发发送
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener('input', autoResize);
autoResize();
