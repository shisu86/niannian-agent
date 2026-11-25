/**
 * POST + ReadableStream 解析 SSE。
 * EventSource 不支持 POST，所以必须用 fetch 读取流。
 */

function parseEvent(block) {
  const lines = block.split('\n');
  const event = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
  const data = lines
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trim())
    .join('\n');

  if (!event) return null;

  try {
    return { event, data: JSON.parse(data) };
  } catch {
    return { event, data: {} };
  }
}

/**
 * @param {{thread_id: string, message: string}} payload
 * @param {Record<string, (data: any) => void>} handlers
 */
export async function chatStream(payload, handlers = {}) {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      message = body?.error?.message || message;
    } catch {
      // 保留默认提示
    }
    throw new Error(message);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    let index;
    while ((index = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);

      const event = parseEvent(block);
      if (!event) continue;

      handlers[event.event]?.(event.data);

      // done 或 error 都是本轮终点，提前释放连接
      if (event.event === 'done' || event.event === 'error') {
        reader.cancel();
        return;
      }
    }
  }

  // 读到 EOF 仍未收到 done/error：连接被提前关闭，不能当作成功
  throw new Error('连接中断，请重试');
}
