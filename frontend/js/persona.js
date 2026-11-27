import { requestJSON } from './api.js';

const modal = document.getElementById('persona-modal');
const stepInput = document.getElementById('persona-step-input');
const stepConfirm = document.getElementById('persona-step-confirm');
const dialogEl = document.getElementById('persona-dialog');
const nameHintEl = document.getElementById('persona-name');
const relationHintEl = document.getElementById('persona-relation');
const statusEl = document.getElementById('persona-status');
const confirmStatusEl = document.getElementById('persona-confirm-status');
const extractBtn = document.getElementById('persona-extract');
const confirmBtn = document.getElementById('persona-confirm');
const defaultBtn = document.getElementById('persona-default');

let personaThreadId = '';

function setStatus(el, text) {
  el.textContent = text || '';
}

function openModal() {
  modal.hidden = false;
  stepInput.hidden = false;
  stepConfirm.hidden = true;
  extractBtn.disabled = false;
  confirmBtn.disabled = false;
  defaultBtn.disabled = false;
  setStatus(statusEl, '');
  setStatus(confirmStatusEl, '');
  dialogEl.focus();
}

function closeModal() {
  modal.hidden = true;
}

// 选手：可选时间戳 + 说话人（最多 20 字）+ 冒号 + 内容
const LINE_PATTERN =
  /^(?:(\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?)\s+)?([^：:]{1,20})[：:]\s*(.+)$/;
const MAX_MESSAGE_LEN = 2000;

/**
 * 解析粘贴的对话。
 * 规则：每行 `说话人：内容`；称呼精确匹配的说话人标为目标。
 * 没填称呼时，把第一个出现的说话人当作目标；
 * 整段没有冒号格式时，当作目标人物说的话。
 * 完整版会调用 LLM 标注说话人，S5 先用规则，覆盖常见粘贴格式。
 */
function parseDialog(text, nameHint) {
  const lines = [];
  const speakers = new Set();

  for (const rawLine of text.split('\n')) {
    const trimmed = rawLine.trim();
    if (!trimmed) continue;

    const match = trimmed.match(LINE_PATTERN);
    if (match) {
      const speaker = match[2].trim();
      speakers.add(speaker);
      lines.push({ speaker, text: match[3].trim().slice(0, MAX_MESSAGE_LEN) });
    } else if (lines.length) {
      // 没有说话人前缀的续行，接到上一条，并限制单条长度
      const last = lines[lines.length - 1];
      last.text = `${last.text}\n${trimmed}`.slice(0, MAX_MESSAGE_LEN);
    } else {
      // 整段没有"说话人："格式：当作要模仿的人说的话
      lines.push({ speaker: '', text: trimmed.slice(0, MAX_MESSAGE_LEN) });
    }
  }

  // 精确匹配优先；再做包含匹配，避免"妈妈"和"妈妈的朋友"同时命中
  const SELF_MARKS = new Set(['我', '自己', '本人', 'me', 'Me', 'ME']);
  let targetSpeaker = '';
  if (nameHint && speakers.has(nameHint)) {
    targetSpeaker = nameHint;
  } else if (nameHint) {
    targetSpeaker =
      [...speakers].find(
        (speaker) => speaker.includes(nameHint) || nameHint.includes(speaker),
      ) || '';
  } else if (lines.length) {
    // 没填称呼：优先选不是"我/自己"的说话人，否则第一行常被选成用户本人
    const firstOther = lines.find(
      (line) => line.speaker && !SELF_MARKS.has(line.speaker),
    );
    targetSpeaker = firstOther ? firstOther.speaker : lines[0].speaker || '';
  }

  return lines.map((line) => ({
    text: line.text,
    is_target: targetSpeaker ? line.speaker === targetSpeaker : line.speaker === '',
  }));
}

function fillForm(candidates) {
  document.getElementById('p-name').value = candidates.name || '';
  document.getElementById('p-relation').value = candidates.relation || '';
  document.getElementById('p-traits').value = (candidates.traits || []).join('，');
  document.getElementById('p-catchphrases').value = (candidates.catchphrases || []).join('，');
  document.getElementById('p-values').value = (candidates.values || []).join('，');
  document.getElementById('p-care').value = candidates.care_style || '';
  document.getElementById('p-prompt').value = candidates.system_prompt || '';
}

function splitList(value) {
  return value
    .split(/[，,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function handleExtract() {
  const text = dialogEl.value.trim();
  const nameHint = nameHintEl.value.trim();
  const relationHint = relationHintEl.value.trim();
  const messages = parseDialog(text, nameHint);

  if (!messages.length) {
    setStatus(statusEl, '请先粘贴一段对话');
    return;
  }
  if (messages.length > 500) {
    setStatus(statusEl, '对话太长了，最多 500 条');
    return;
  }
  if (!messages.some((item) => item.is_target)) {
    setStatus(statusEl, '没识别到要模仿的人，请填写称呼或检查对话格式');
    return;
  }

  extractBtn.disabled = true;
  setStatus(statusEl, '正在提取，可能要几秒…');
  try {
    const data = await requestJSON('/api/persona/extract', {
      method: 'POST',
      body: JSON.stringify({
        messages,
        name_hint: nameHint,
        relation_hint: relationHint,
      }),
    });
    personaThreadId = data.persona_thread_id;
    fillForm(data.candidates);
    stepInput.hidden = true;
    stepConfirm.hidden = false;
    setStatus(confirmStatusEl, '');
  } catch (error) {
    setStatus(statusEl, error.message || '提取失败');
  } finally {
    extractBtn.disabled = false;
  }
}

async function handleConfirm() {
  const persona = {
    name: document.getElementById('p-name').value.trim(),
    relation: document.getElementById('p-relation').value.trim(),
    traits: splitList(document.getElementById('p-traits').value),
    catchphrases: splitList(document.getElementById('p-catchphrases').value),
    values: splitList(document.getElementById('p-values').value),
    care_style: document.getElementById('p-care').value.trim(),
    system_prompt: document.getElementById('p-prompt').value.trim(),
  };

  let saved = false;
  confirmBtn.disabled = true;
  setStatus(confirmStatusEl, '正在保存…');
  try {
    const result = await requestJSON('/api/persona/confirm', {
      method: 'POST',
      body: JSON.stringify({ persona_thread_id: personaThreadId, persona }),
    });
    saved = true;
    setStatus(confirmStatusEl, '已保存');
    // 让左侧角色列表和右侧人设卡片立即刷新
    document.dispatchEvent(
      new CustomEvent('niannian:persona-changed', {
        detail: { personaId: result.persona?.id },
      }),
    );
    setTimeout(closeModal, 600);
  } catch (error) {
    if (error.code === 'INTERRUPT_NOT_PENDING') {
      // 确认已过期或已提交过：退回第一步，避免继续用失效的 id
      personaThreadId = '';
      stepConfirm.hidden = true;
      stepInput.hidden = false;
      setStatus(confirmStatusEl, '');
      setStatus(statusEl, '确认已失效，请重新提取');
    } else {
      setStatus(confirmStatusEl, error.message || '保存失败');
    }
  } finally {
    // 成功后保持禁用，避免弹窗关闭前再次点击
    confirmBtn.disabled = saved;
  }
}

async function handleDefault() {
  defaultBtn.disabled = true;
  setStatus(statusEl, '正在使用默认人设…');
  try {
    const result = await requestJSON('/api/persona/default', { method: 'POST', body: '{}' });
    setStatus(statusEl, '已使用默认人设');
    document.dispatchEvent(
      new CustomEvent('niannian:persona-changed', {
        detail: { personaId: result.persona?.id },
      }),
    );
    setTimeout(closeModal, 600);
  } catch (error) {
    setStatus(statusEl, error.message || '操作失败');
  } finally {
    defaultBtn.disabled = false;
  }
}

// 顶栏不再放“人设”按钮：新建会话时由 chat.js 派发事件打开
document.addEventListener('niannian:new-persona', openModal);
document.getElementById('persona-close').addEventListener('click', closeModal);
extractBtn.addEventListener('click', handleExtract);
confirmBtn.addEventListener('click', handleConfirm);
defaultBtn.addEventListener('click', handleDefault);
document.getElementById('persona-back').addEventListener('click', () => {
  stepConfirm.hidden = true;
  stepInput.hidden = false;
});

modal.addEventListener('click', (event) => {
  if (event.target === modal) closeModal();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !modal.hidden) closeModal();
});
