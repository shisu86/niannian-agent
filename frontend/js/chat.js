import { chatStream } from './sse.js';

const STORAGE_KEY = 'niannian.thread_id';
const SESSIONS_KEY = 'niannian.sessions';
const THREAD_ID_PATTERN = /^[A-Za-z0-9_-]{8,64}$/;
const MAX_SESSIONS = 30;

const messagesEl = document.getElementById('messages');
const form = document.getElementById('composer');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');
const sessionListEl = document.getElementById('session-list');
const sessionSearchEl = document.getElementById('session-search');
const chatTitleEl = document.getElementById('chat-title');
const chatSubtitleEl = document.getElementById('chat-subtitle');
const chatAvatarEl = document.getElementById('chat-avatar');
const carePill = document.getElementById('care-pill');
const memoryContextEl = document.getElementById('memory-context');
const devBranch = document.getElementById('dev-branch');
const devEmotion = document.getElementById('dev-emotion');
const devElapsed = document.getElementById('dev-elapsed');
const devTokens = document.getElementById('dev-tokens');
const devQueries = document.getElementById('dev-queries');
const personaNameEl = document.getElementById('persona-card-name');
const personaRelationEl = document.getElementById('persona-card-relation');
const personaCatchphrasesEl = document.getElementById('persona-card-catchphrases');
const personaAvatarEl = document.getElementById('persona-card-avatar');
const personaSummaryEl = document.getElementById('persona-card-summary');
const leftPersonaName = document.getElementById('left-persona-name');
const memoryCountEl = document.getElementById('memory-count');
const devSummaryEl = document.getElementById('dev-summary');

const TYPE_LABELS = {
  fact: '事实',
  preference: '偏好',
  event: '事件',
  relationship: '关系',
  followup: '待跟进',
  summary: '摘要',
};

let sending = false;
let threadId = null;
let lastTokenCount = 0;
let personas = [];
let sessions = loadSessions();

function loadSessions() {
  try {
    const raw = JSON.parse(localStorage.getItem(SESSIONS_KEY) || '[]');
    if (Array.isArray(raw)) {
      return raw
        .filter((item) => item && THREAD_ID_PATTERN.test(item.id))
        .slice(0, MAX_SESSIONS)
        .map((item) => ({
          id: item.id,
          persona_id: item.persona_id || null,
          title: item.title === '新会话' ? '' : item.title || '',
          subtitle: item.subtitle || '',
          empty: Boolean(item.empty),
          unread: Boolean(item.unread),
          created_at: item.created_at || new Date().toISOString(),
        }));
    }
  } catch {
    // 本地数据损坏时重新开始
  }
  return [];
}

function saveSessions() {
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions.slice(0, MAX_SESSIONS)));
}

function newThreadId() {
  const random = crypto.randomUUID?.() || `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return random.replace(/-/g, '');
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function formatClock(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function dayLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '今天';
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return '今天';
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return '昨天';
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

function personaById(personaId) {
  return personas.find((persona) => persona.id === personaId) || personas[0] || null;
}

function sessionPersonaId(session) {
  return session?.persona_id || personas[0]?.id || 1;
}

function currentPersonaId() {
  const session = sessions.find((item) => item.id === threadId);
  return sessionPersonaId(session);
}

async function loadPersonas() {
  try {
    const response = await fetch('/api/personas');
    const body = await response.json();
    personas = body?.data?.items || [];
  } catch {
    personas = [];
  }
}

function ensureSession() {
  // 旧会话没有保存角色：统一挂到第一个角色下
  const fallbackPersona = personas[0]?.id || 1;
  if (localStorage.getItem('niannian.sessions_migrated') !== '1') {
    const savedName = localStorage.getItem('niannian.persona_name') || '念念';
    for (const session of sessions) {
      if (!session.title) session.title = savedName;
      if (!session.persona_id) session.persona_id = fallbackPersona;
    }
    saveSessions();
    localStorage.setItem('niannian.sessions_migrated', '1');
  } else {
    for (const session of sessions) {
      if (!session.persona_id) session.persona_id = fallbackPersona;
    }
    saveSessions();
  }

  const stored = localStorage.getItem(STORAGE_KEY);
  let id = stored && THREAD_ID_PATTERN.test(stored) ? stored : null;

  if (!id) {
    id = sessions[0]?.id || newThreadId();
  }
  if (!sessions.some((session) => session.id === id)) {
    sessions.unshift({
      id,
      persona_id: fallbackPersona,
      title: personas[0]?.name || '',
      subtitle: '',
      empty: true,
      created_at: new Date().toISOString(),
    });
    saveSessions();
  }

  threadId = id;
  localStorage.setItem(STORAGE_KEY, id);
}

function sessionDayLabel(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '更早';
  return date.toDateString() === new Date().toDateString() ? '今天' : '更早';
}

function renderSessions() {
  const keyword = sessionSearchEl.value.trim().toLowerCase();
  sessionListEl.replaceChildren();

  const sorted = [...sessions].sort(
    (a, b) => new Date(b.created_at) - new Date(a.created_at),
  );

  let lastLabel = '';
  for (const session of sorted) {
    const persona = personaById(sessionPersonaId(session));
    const displayTitle = session.title || persona?.name || '新会话';
    if (keyword && !displayTitle.toLowerCase().includes(keyword)) continue;

    const label = sessionDayLabel(session.created_at);
    if (label !== lastLabel) {
      const heading = document.createElement('div');
      heading.className = 'session-day';
      heading.textContent = label;
      sessionListEl.append(heading);
      lastLabel = label;
    }

    sessionListEl.append(renderSessionItem(session, displayTitle, persona));
  }
}

function renderSessionItem(session, displayTitle, persona) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `session-item${session.id === threadId ? ' active' : ''}`;

  const avatar = document.createElement('span');
  avatar.className = 'session-avatar';
  avatar.textContent = (persona?.name || displayTitle || '念').slice(0, 1);

  const info = document.createElement('span');
  info.className = 'session-info';

  const title = document.createElement('span');
  title.className = 'title';
  title.textContent = displayTitle;

  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = session.subtitle || (session.empty ? '还没有聊过' : formatDate(session.created_at));

  info.append(title, meta);

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'session-delete';
  remove.textContent = '×';
  remove.setAttribute('aria-label', '删除会话');
  remove.addEventListener('click', (event) => {
    event.stopPropagation();
    deleteSession(session.id);
  });

  button.append(avatar, info);

  if (session.unread) {
    const dot = document.createElement('span');
    dot.className = 'session-dot';
    dot.setAttribute('aria-label', '未读');
    button.append(dot);
  }

  button.append(remove);
  button.addEventListener('click', () => switchSession(session.id));
  return button;
}

function updateChatHeader() {
  const session = sessions.find((item) => item.id === threadId);
  const persona = personaById(sessionPersonaId(session));
  const name = session?.title || persona?.name || '新会话';
  chatTitleEl.textContent = name;
  chatAvatarEl.textContent = name.slice(0, 1);
  chatSubtitleEl.textContent = session?.subtitle || (session?.empty ? '还没有聊过' : '');
}

function markSessionUsed(text) {
  const session = sessions.find((item) => item.id === threadId);
  if (!session) return;
  session.empty = false;
  session.subtitle = text.slice(0, 16);
  saveSessions();
  renderSessions();
  updateChatHeader();
}

function createEmptyState() {
  const wrapper = document.createElement('div');
  wrapper.className = 'empty';
  wrapper.id = 'empty';
  const line = document.createElement('p');
  line.textContent = '今天想聊点什么？';
  const hint = document.createElement('p');
  hint.className = 'hint';
  hint.textContent = '我会记得你说过的事。';
  wrapper.append(line, hint);
  return wrapper;
}

function resetChatView() {
  messagesEl.replaceChildren(createEmptyState());
  memoryContextEl.replaceChildren();
  const hint = document.createElement('p');
  hint.className = 'hint';
  hint.textContent = '还没有检索记录。';
  memoryContextEl.append(hint);
  devBranch.textContent = '-';
  devEmotion.textContent = '-';
  devElapsed.textContent = '-';
  devTokens.textContent = '-';
  devQueries.textContent = '-';
}

async function registerThread(id = threadId) {
  const session = sessions.find((item) => item.id === id);
  try {
    await fetch('/api/session/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        thread_id: id,
        persona_id: sessionPersonaId(session),
      }),
    });
  } catch {
    // 注册失败不影响聊天
  }
}

async function loadHistory() {
  const id = threadId;
  try {
    await registerThread(id);
    const response = await fetch(`/api/session/${id}/messages?limit=50`);
    const body = await response.json();
    // 请求期间切换了会话，丢弃这次结果
    if (id !== threadId) return;

    const items = body?.data?.items || [];
    for (const item of items) {
      addBubble(item.role === 'user' ? 'me' : 'ai', item.content, {
        proactive: item.name === 'proactive',
        time: item.created_at,
      });
    }
    updateChatHeader();
  } catch {
    // 历史加载失败时保持空会话
  }
}

async function switchSession(id) {
  if (sending) return;
  const session = sessions.find((item) => item.id === id);
  if (session?.unread) {
    session.unread = false;
    saveSessions();
    renderSessions();
  }
  if (id === threadId) return;
  threadId = id;
  localStorage.setItem(STORAGE_KEY, id);
  renderSessions();
  updateChatHeader();
  loadPersonaCardForCurrent();
  resetChatView();
  await loadHistory();
}

async function createSessionFor(personaId, force = false) {
  if (sending) return;
  const persona = personaById(personaId);
  if (!persona) return;

  if (!force) {
    const emptySession = sessions.find(
      (session) => session.empty && sessionPersonaId(session) === persona.id,
    );
    if (emptySession) {
      if (emptySession.id === threadId) {
        setStatus('当前已经是新会话');
        return;
      }
      await switchSession(emptySession.id);
      return;
    }
  }

  const id = newThreadId();
  sessions.unshift({
    id,
    persona_id: persona.id,
    title: persona.name,
    subtitle: '',
    empty: true,
    created_at: new Date().toISOString(),
  });
  saveSessions();
  threadId = id;
  localStorage.setItem(STORAGE_KEY, id);
  renderSessions();
  updateChatHeader();
  loadPersonaCardForCurrent();
  resetChatView();
  await registerThread();
  input.focus();
}

async function deleteSession(id) {
  if (sending) return;
  const session = sessions.find((item) => item.id === id);
  const personaId = session?.persona_id;
  if (
    !window.confirm(
      '删除这个会话？如果这是该角色的最后一个会话，角色和它的专属记忆也会一起删除。',
    )
  ) {
    return;
  }

  try {
    await fetch(`/api/session/${id}`, { method: 'DELETE' });
  } catch {
    // 后端删除失败也清理本地，避免列表卡住
  }

  sessions = sessions.filter((item) => item.id !== id);
  saveSessions();

  // 这个角色已经没有任何会话时，连同角色一起删除，
  // 否则“选择聊天对象”里还会留着这个人物。
  const personaHasSessions = sessions.some((item) => item.persona_id === personaId);
  if (personaId && !personaHasSessions) {
    try {
      await fetch(`/api/personas/${personaId}`, { method: 'DELETE' });
    } catch {
      // 角色删除失败不影响会话清理
    }
    await loadPersonas();
  }

  if (id !== threadId) {
    renderSessions();
    return;
  }

  const next = sessions[0];
  if (next) {
    threadId = next.id;
    localStorage.setItem(STORAGE_KEY, threadId);
    renderSessions();
    updateChatHeader();
    loadPersonaCardForCurrent();
    resetChatView();
    await loadHistory();
  } else if (personas.length) {
    await createSessionFor(personas[0].id, true);
  } else {
    // 一个角色都不剩：清空界面，直接引导新建角色
    threadId = null;
    localStorage.removeItem(STORAGE_KEY);
    renderSessions();
    resetChatView();
    updateChatHeader();
    openRoleCreate();
  }
}

async function deletePersona(personaId) {
  if (sending) return;
  if (!window.confirm('删除这个角色？它的会话和专属记忆也会一起删除。')) return;

  try {
    await fetch(`/api/personas/${personaId}`, { method: 'DELETE' });
  } catch {
    setStatus('删除角色失败');
    return;
  }

  sessions = sessions.filter((session) => session.persona_id !== personaId);
  saveSessions();
  await loadPersonas();

  if (!personas.length) {
    // 一个角色都不剩：清空界面、禁用输入并引导新建
    threadId = null;
    localStorage.removeItem(STORAGE_KEY);
    sessions = [];
    saveSessions();
    renderSessions();
    resetChatView();
    updateChatHeader();
    setComposerEnabled(false);
    openRoleCreate();
    return;
  }

  const currentExists = sessions.some((session) => session.id === threadId);
  if (!currentExists) {
    const next = sessions[0];
    if (next) {
      threadId = next.id;
      localStorage.setItem(STORAGE_KEY, threadId);
      renderSessions();
      updateChatHeader();
      loadPersonaCardForCurrent();
      resetChatView();
      await loadHistory();
    } else {
      await createSessionFor(personas[0].id, true);
    }
  } else {
    renderSessions();
  }

  if (!pickerEl.hidden) renderPicker();
}

const pickerEl = document.getElementById('persona-picker');
const pickerListEl = document.getElementById('picker-list');

function renderPicker() {
  pickerListEl.replaceChildren();
  for (const persona of personas) {
    const row = document.createElement('div');
    row.className = 'picker-item';

    const avatar = document.createElement('span');
    avatar.className = 'session-avatar';
    avatar.textContent = (persona.name || '念').slice(0, 1);

    const info = document.createElement('div');
    info.className = 'picker-info';

    const name = document.createElement('p');
    name.className = 'picker-name';
    name.textContent = persona.name || '未命名角色';

    const relation = document.createElement('p');
    relation.className = 'hint';
    relation.textContent = persona.relation || '';

    info.append(name, relation);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'session-delete';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `删除角色${persona.name}`);
    remove.addEventListener('click', (event) => {
      event.stopPropagation();
      deletePersona(persona.id);
    });

    row.append(avatar, info, remove);
    row.addEventListener('click', () => {
      closePicker();
      createSessionFor(persona.id, true);
    });
    pickerListEl.append(row);
  }
}

function openPicker() {
  // 还没有任何角色时，直接进入新建角色，不预置任何人物
  if (!personas.length) {
    openRoleCreate();
    return;
  }
  renderPicker();
  pickerEl.hidden = false;
}

function closePicker() {
  pickerEl.hidden = true;
}

document.getElementById('picker-close').addEventListener('click', closePicker);
document.getElementById('picker-new').addEventListener('click', openRoleCreate);

const roleCreateModal = document.getElementById('role-create-modal');
const roleNameInput = document.getElementById('role-name');
const roleRelationInput = document.getElementById('role-relation');

function openRoleCreate() {
  closePicker();
  roleNameInput.value = '';
  roleRelationInput.value = '';
  roleCreateModal.hidden = false;
  roleNameInput.focus();
}

function closeRoleCreate() {
  roleCreateModal.hidden = true;
}

async function createBlankPersona() {
  const name = roleNameInput.value.trim() || '新朋友';
  const relation = roleRelationInput.value.trim() || '朋友';

  try {
    const response = await fetch('/api/personas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name,
        relation,
        traits: [],
        catchphrases: [],
        values: [],
        care_style: '',
        system_prompt: '',
      }),
    });
    if (!response.ok) throw new Error('create failed');

    const body = await response.json();
    const persona = body?.data?.persona;
    closeRoleCreate();
    await loadPersonas();
    setComposerEnabled(true);
    if (persona?.id) {
      await createSessionFor(persona.id, true);
    } else {
      renderSessions();
    }
  } catch {
    setStatus('创建角色失败');
  }
}

document.getElementById('role-create-submit').addEventListener('click', createBlankPersona);
document.getElementById('role-create-close').addEventListener('click', closeRoleCreate);
document.getElementById('role-create-advanced').addEventListener('click', () => {
  closeRoleCreate();
  document.dispatchEvent(new CustomEvent('niannian:new-persona'));
});
roleCreateModal.addEventListener('click', (event) => {
  if (event.target === roleCreateModal) closeRoleCreate();
});
pickerEl.addEventListener('click', (event) => {
  if (event.target === pickerEl) closePicker();
});

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function setStatus(text) {
  statusEl.textContent = text;
}

function addBubble(role, text = '', options = {}) {
  document.getElementById('empty')?.remove();

  // 第一轮消息前加一条日期分隔，按首条消息的时间显示今天/昨天/日期
  if (!messagesEl.querySelector('.day-divider')) {
    const divider = document.createElement('div');
    divider.className = 'day-divider';
    divider.textContent = dayLabel(options.time);
    messagesEl.prepend(divider);
  }

  const row = document.createElement('div');
  row.className = `msg ${role}`;

  const avatar = document.createElement('span');
  avatar.className = 'msg-avatar';
  if (role === 'me') {
    avatar.textContent = '我';
  } else {
    const persona = personaById(currentPersonaId());
    avatar.textContent = (persona?.name || '念').slice(0, 1);
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  // textContent 而不是 innerHTML：用户输入和模型输出都可能包含 HTML
  bubble.textContent = text;
  if (options.proactive) bubble.classList.add('proactive');

  row.append(avatar, bubble);

  if (options.proactive) {
    const note = document.createElement('span');
    note.className = 'proactive-note';
    note.textContent = '· 主动关怀';
    row.append(note);
  }

  if (options.time) {
    const time = document.createElement('span');
    time.className = 'msg-time';
    time.textContent = formatClock(options.time);
    row.append(time);
  }

  messagesEl.appendChild(row);
  scrollToBottom();
  return bubble;
}

function renderMemoryTags(bubble, items) {
  if (!items.length) return;
  const row = bubble.parentElement;
  if (!row) return;

  const tags = document.createElement('div');
  tags.className = 'memory-tags';
  for (const item of items.slice(0, 3)) {
    const tag = document.createElement('span');
    tag.className = 'memory-tag';
    tag.textContent = `${TYPE_LABELS[item.type] || item.type} · ${String(item.content).slice(0, 12)}`;
    tags.append(tag);
  }
  row.append(tags);

  const note = document.createElement('p');
  note.className = 'memory-note';
  note.textContent = `它想起了 ${items.length} 件事`;
  row.append(note);
}

function renderMemoryContext(items) {
  memoryContextEl.replaceChildren();
  memoryCountEl.textContent = `${items.length} 件事`;

  if (!items.length) {
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '这轮没有命中记忆。';
    memoryContextEl.append(hint);
    return;
  }

  for (const item of items) {
    const card = document.createElement('div');
    card.className = 'memory-card';

    const head = document.createElement('div');
    head.className = 'memory-card-head';

    const tag = document.createElement('span');
    tag.className = 'memory-type';
    tag.textContent = TYPE_LABELS[item.type] || item.type;

    const meta = document.createElement('span');
    meta.className = 'memory-meta';
    meta.textContent = item.hit_count ? `想起过 ${item.hit_count} 次` : '';

    head.append(tag, meta);

    const text = document.createElement('p');
    text.className = 'memory-content';
    text.textContent = item.content;

    card.append(head, text);
    memoryContextEl.append(card);
  }
}

function renderDevTrace(data) {
  devBranch.textContent = data.branch || '-';
  devEmotion.textContent = data.emotion || '-';
  devElapsed.textContent = data.elapsed_ms ? `${data.elapsed_ms} ms` : '-';
  devQueries.textContent = (data.queries || []).join(' / ') || '-';
  devSummaryEl.textContent = [
    data.branch || '-',
    `${lastTokenCount} token`,
    data.elapsed_ms ? `${data.elapsed_ms}ms` : '-',
  ].join(' · ');
}

function renderChips(element, values) {
  element.replaceChildren();
  for (const value of values.slice(0, 6)) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = value;
    element.append(chip);
  }
}

function loadPersonaCardForCurrent() {
  const persona = personaById(currentPersonaId());
  if (!persona) return;

  const name = persona.name || '念念';
  personaNameEl.textContent = name;
  personaRelationEl.textContent = persona.relation || '朋友';
  leftPersonaName.textContent = name;
  personaAvatarEl.textContent = name.slice(0, 1);

  const summaryParts = [];
  if (persona.traits?.length) summaryParts.push(persona.traits.join('，'));
  if (persona.care_style) summaryParts.push(persona.care_style);
  personaSummaryEl.textContent = summaryParts.join('。');

  renderChips(personaCatchphrasesEl, persona.catchphrases || []);

  // 通知语音模块：音色跟着角色切换
  document.dispatchEvent(
    new CustomEvent('niannian:session-changed', {
      detail: { personaId: persona.id },
    }),
  );
}

async function handlePersonaChanged(event) {
  const oldIds = new Set(personas.map((persona) => persona.id));
  await loadPersonas();
  setComposerEnabled(true);

  const changedId = event.detail?.personaId;
  if (changedId && !oldIds.has(changedId)) {
    // 新增角色：为它开一个新会话
    await createSessionFor(changedId, true);
  } else {
    renderSessions();
    updateChatHeader();
    loadPersonaCardForCurrent();
  }
}

function autoResize() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
}

function setComposerEnabled(enabled) {
  input.disabled = !enabled;
  sendBtn.disabled = !enabled;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();

  const text = input.value.trim();
  if (!text || sending) return;

  sending = true;
  sendBtn.disabled = true;
  setStatus('正在想…');
  lastTokenCount = 0;

  markSessionUsed(text);
  addBubble('me', text, { time: new Date().toISOString() });
  input.value = '';
  autoResize();

  const bubble = addBubble('ai', '', { time: new Date().toISOString() });
  bubble.classList.add('typing');

  try {
    await chatStream(
      { thread_id: threadId, message: text },
      {
        token(data) {
          lastTokenCount += 1;
          bubble.textContent += data.text || '';
          scrollToBottom();
        },
        memory(data) {
          const items = data.items || [];
          renderMemoryTags(bubble, items);
          renderMemoryContext(items);
        },
        trace(data) {
          if (data.branch === 'safety') {
            bubble.classList.add('safety');
          }
          if (data.emotion) bubble.dataset.emotion = data.emotion;
          renderDevTrace(data);
          devTokens.textContent = String(lastTokenCount);
        },
        error(data) {
          setStatus(data.message || '出错了');
          bubble.dataset.failed = '1';
          if (!bubble.textContent) bubble.textContent = '（没有收到回复）';
        },
        done() {
          bubble.classList.remove('typing');
          setStatus('');
          const timeNode = bubble.parentElement?.querySelector('.msg-time');
          if (timeNode) timeNode.textContent = formatClock(new Date().toISOString());
          devTokens.textContent = String(lastTokenCount);
          devSummaryEl.textContent = devSummaryEl.textContent.replace(
            /\d+ token/,
            `${lastTokenCount} token`,
          );
          document.dispatchEvent(
            new CustomEvent('niannian:reply-done', { detail: { bubble } }),
          );
        },
      },
    );
  } catch (error) {
    setStatus(error.message || '网络错误');
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
sessionSearchEl.addEventListener('input', renderSessions);
document.getElementById('session-new').addEventListener('click', openPicker);
document.addEventListener('niannian:persona-changed', handlePersonaChanged);

async function loadCarePill() {
  try {
    const response = await fetch('/api/settings');
    const body = await response.json();
    const settings = body?.data?.settings;
    if (!settings) return;
    carePill.textContent = `关怀 · ${settings.quiet_start}-${settings.quiet_end} 安静`;
  } catch {
    // 读取失败时保留默认文案
  }
}

carePill.addEventListener('click', () => {
  document.dispatchEvent(new CustomEvent('niannian:open-settings'));
});

// 收到主动关怀通知时，在对应会话上显示未读红点
document.addEventListener('niannian:notifications', (event) => {
  const items = event.detail?.items || [];
  let changed = false;
  for (const item of items) {
    const session = sessions.find((entry) => entry.id === item.thread_id);
    // 当前正在看的会话，消息已经显示在聊天区，不需要红点
    if (session && session.id !== threadId && !session.unread) {
      session.unread = true;
      changed = true;
    }
  }
  if (changed) {
    saveSessions();
    renderSessions();
  }
});

function pruneOrphanSessions() {
  if (!personas.length) return;
  const valid = new Set(personas.map((persona) => persona.id));
  const before = sessions.length;
  sessions = sessions.filter((session) => valid.has(sessionPersonaId(session)));
  if (sessions.length !== before) saveSessions();
}

(async () => {
  await loadPersonas();
  pruneOrphanSessions();

  if (!personas.length) {
    // 没有任何角色：禁用输入，直接引导新建，不创建幽灵会话
    setComposerEnabled(false);
    renderSessions();
    resetChatView();
    updateChatHeader();
    loadCarePill();
    openRoleCreate();
    return;
  }

  setComposerEnabled(true);
  ensureSession();
  await cleanupEmptySessions();
  renderSessions();
  updateChatHeader();
  loadPersonaCardForCurrent();
  resetChatView();
  await loadHistory();
  loadCarePill();
  autoResize();
})();

async function cleanupEmptySessions() {
  const empties = sessions.filter((session) => session.empty);
  if (empties.length <= 1) return;

  const keep = empties.find((session) => session.id === threadId) || empties[0];
  for (const session of empties) {
    if (session.id === keep.id) continue;
    try {
      await fetch(`/api/session/${session.id}`, { method: 'DELETE' });
    } catch {
      // 删除失败也继续
    }
  }
  sessions = sessions.filter((session) => !session.empty || session.id === keep.id);
  saveSessions();
}
