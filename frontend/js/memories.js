import { requestJSON } from './api.js';

const modal = document.getElementById('memories-modal');
const listEl = document.getElementById('memories-list');
const followupsEl = document.getElementById('followups-list');
const settingsPanel = document.getElementById('settings-panel');
const statusEl = document.getElementById('memories-status');
const tabMemories = document.getElementById('tab-memories');
const tabFollowups = document.getElementById('tab-followups');
const tabSettings = document.getElementById('tab-settings');
const settingsSave = document.getElementById('settings-save');

const TYPE_LABELS = {
  fact: '事实',
  preference: '偏好',
  event: '事件',
  relationship: '关系',
  followup: '待跟进',
  summary: '摘要',
};

function setStatus(text) {
  statusEl.textContent = text || '';
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

function formatLocal(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (number) => String(number).padStart(2, '0');
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function itemRow(type, content, extra, scope) {
  const row = document.createElement('div');
  row.className = 'memory-item';

  const tag = document.createElement('span');
  tag.className = 'memory-type';
  tag.textContent = TYPE_LABELS[type] || type;
  if (scope === 'shared') tag.textContent += ' · 共享';

  const text = document.createElement('p');
  text.className = 'memory-content';
  // 记忆内容可能来自模型/用户输入，统一用 textContent
  text.textContent = content;

  row.append(tag, text);
  if (extra) row.append(extra);
  return row;
}

function deleteButton(memoryId) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'ghost small';
  button.textContent = '删除';
  button.addEventListener('click', async () => {
    if (!window.confirm('删除这条记忆？')) return;
    button.disabled = true;
    try {
      await requestJSON(
        `/api/memories/${memoryId}?persona_id=${currentPersonaId()}`,
        { method: 'DELETE' },
      );
      await loadMemories();
    } catch (error) {
      setStatus(error.message || '删除失败');
      button.disabled = false;
    }
  });
  return button;
}

function openEditor(item, row) {
  const form = document.createElement('div');
  form.className = 'memory-item memory-edit';

  const textarea = document.createElement('textarea');
  textarea.rows = 2;
  textarea.maxLength = 1000;
  textarea.value = item.content;

  const importance = document.createElement('select');
  for (let value = 1; value <= 5; value += 1) {
    const option = document.createElement('option');
    option.value = String(value);
    option.textContent = `重要度 ${value}`;
    importance.append(option);
  }
  importance.value = String(item.importance || 3);

  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'small';
  save.textContent = '保存';

  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'ghost small';
  cancel.textContent = '取消';

  const actions = document.createElement('div');
  actions.className = 'memory-actions';
  actions.append(save, cancel);
  form.append(textarea, importance, actions);
  row.replaceWith(form);

  cancel.addEventListener('click', () => form.replaceWith(row));
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await requestJSON(
        `/api/memories/${item.id}?persona_id=${currentPersonaId()}`,
        {
          method: 'PUT',
          body: JSON.stringify({
            content: textarea.value.trim(),
            importance: Number(importance.value),
          }),
        },
      );
      await loadMemories();
    } catch (error) {
      setStatus(error.message || '保存失败');
      save.disabled = false;
    }
  });
}

function memoryRow(item) {
  const row = itemRow(item.type, item.content, null, item.scope);
  const actions = document.createElement('div');
  actions.className = 'memory-actions';

  const edit = document.createElement('button');
  edit.type = 'button';
  edit.className = 'ghost small';
  edit.textContent = '编辑';
  edit.addEventListener('click', () => openEditor(item, row));

  actions.append(edit, deleteButton(item.id));
  row.append(actions);
  return row;
}

async function loadMemories() {
  setStatus('');
  try {
    const data = await requestJSON(
      `/api/memories?status=active&limit=100&persona_id=${currentPersonaId()}`,
    );
    listEl.replaceChildren();
    if (!data.items.length) {
      const empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = '还没有记住什么。';
      listEl.append(empty);
      return;
    }
    for (const item of data.items) {
      listEl.append(memoryRow(item));
    }
  } catch (error) {
    setStatus(error.message || '加载记忆失败');
  }
}

async function loadFollowups() {
  setStatus('');
  try {
    const data = await requestJSON(`/api/followups?persona_id=${currentPersonaId()}`);
    followupsEl.replaceChildren();
    if (!data.items.length) {
      const empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = '暂时没有待跟进的事。';
      followupsEl.append(empty);
      return;
    }
    for (const item of data.items) {
      const meta = document.createElement('span');
      meta.className = 'memory-meta';
      meta.textContent = item.due_at ? formatLocal(item.due_at) : '';
      followupsEl.append(itemRow(item.type, item.content, meta, item.scope));
    }
  } catch (error) {
    setStatus(error.message || '加载待跟进失败');
  }
}

function fillSelect(id, value) {
  const element = document.getElementById(id);
  element.value = String(value);
}

async function loadSettings() {
  setStatus('');
  try {
    const data = await requestJSON('/api/settings');
    const settings = data.settings;
    fillSelect('set-proactive', settings.proactive_enabled);
    document.getElementById('set-quiet-start').value = settings.quiet_start || '22:00';
    document.getElementById('set-quiet-end').value = settings.quiet_end || '08:00';
    fillSelect('set-daily-limit', settings.daily_limit);
    fillSelect('set-low-mood', settings.low_mood_pause);
    document.getElementById('set-voice').value = settings.voice_name || '';
    fillSelect('set-auto-read', settings.auto_read);
    fillSelect('set-asr', settings.asr_enabled);
  } catch (error) {
    setStatus(error.message || '加载设置失败');
  }
}

async function saveSettings() {
  const payload = {
    proactive_enabled: Number(document.getElementById('set-proactive').value),
    quiet_start: document.getElementById('set-quiet-start').value.trim(),
    quiet_end: document.getElementById('set-quiet-end').value.trim(),
    daily_limit: Number(document.getElementById('set-daily-limit').value),
    low_mood_pause: Number(document.getElementById('set-low-mood').value),
    voice_name: document.getElementById('set-voice').value.trim(),
    auto_read: Number(document.getElementById('set-auto-read').value),
    asr_enabled: Number(document.getElementById('set-asr').value),
  };

  settingsSave.disabled = true;
  setStatus('正在保存…');
  try {
    const data = await requestJSON('/api/settings', {
      method: 'PUT',
      body: JSON.stringify(payload),
    });
    setStatus('设置已保存');
    document.dispatchEvent(
      new CustomEvent('niannian:settings-changed', {
        detail: { settings: data.settings },
      }),
    );
  } catch (error) {
    setStatus(error.message || '保存失败');
  } finally {
    settingsSave.disabled = false;
  }
}

function switchTab(tab) {
  const isMemories = tab === 'memories';
  const isFollowups = tab === 'followups';
  tabMemories.classList.toggle('active', isMemories);
  tabFollowups.classList.toggle('active', isFollowups);
  tabSettings.classList.toggle('active', !isMemories && !isFollowups);
  listEl.hidden = !isMemories;
  followupsEl.hidden = !isFollowups;
  settingsPanel.hidden = isMemories || isFollowups;
  setStatus('');
  if (isMemories) loadMemories();
  else if (isFollowups) loadFollowups();
  else loadSettings();
}

function openModal() {
  modal.hidden = false;
  switchTab('memories');
}

function closeModal() {
  modal.hidden = true;
}

settingsSave.addEventListener('click', saveSettings);
document.getElementById('memories-open').addEventListener('click', openModal);
document.getElementById('memories-close').addEventListener('click', closeModal);
tabMemories.addEventListener('click', () => switchTab('memories'));
tabFollowups.addEventListener('click', () => switchTab('followups'));
tabSettings.addEventListener('click', () => switchTab('settings'));

modal.addEventListener('click', (event) => {
  if (event.target === modal) closeModal();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !modal.hidden) closeModal();
});

// 顶栏“关怀时段”点击后直接打开设置页
document.addEventListener('niannian:open-settings', () => {
  openModal();
  switchTab('settings');
});
