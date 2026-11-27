import { requestJSON } from './api.js';

const messagesEl = document.getElementById('messages');
const POLL_MS = 30000;

// 服务端读取即标记已读；这里再按 id 去重，防止网络重试造成重复气泡
const seen = new Set();
let timer = null;
let polling = false;

function addProactiveBubble(text) {
  // 每次现查：切换会话后空占位是新建的节点
  document.getElementById('empty')?.remove();

  const row = document.createElement('div');
  row.className = 'msg ai';

  const bubble = document.createElement('div');
  bubble.className = 'bubble proactive';
  // 通知内容来自模型/记忆，同样用 textContent，避免 HTML 注入
  bubble.textContent = text;

  row.appendChild(bubble);
  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    const data = await requestJSON('/api/notifications');
    const items = data.items || [];
    const currentThread = localStorage.getItem('niannian.thread_id');
    for (const item of items) {
      if (!item.content || seen.has(item.id)) continue;
      seen.add(item.id);
      // 其他会话的关怀只标红点，不要插进当前聊天
      if (item.thread_id && item.thread_id !== currentThread) continue;
      addProactiveBubble(item.content);
    }
    if (items.length) {
      // 让会话列表在对应会话上显示未读红点
      document.dispatchEvent(
        new CustomEvent('niannian:notifications', { detail: { items } }),
      );
    }
  } catch {
    // 轮询失败不打扰用户，下一轮再试
  } finally {
    polling = false;
    // 递归 setTimeout 而不是 setInterval：请求慢于间隔时不会重叠
    timer = setTimeout(poll, POLL_MS);
  }
}

// 从后台切回来时立即拉一次，不等下一个 30 秒
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    clearTimeout(timer);
    poll();
  }
});

poll();
