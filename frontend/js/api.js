/**
 * 非流式接口的统一封装。
 * S2 的聊天走 SSE，这里先提供给后续记忆、设置等接口使用。
 */
export async function requestJSON(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    // 合并而不是覆盖，调用方只传额外 header 时不会丢掉 Content-Type
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });

  let body = null;
  try {
    body = await response.json();
  } catch {
    // 非 JSON 响应（如 500 页面）交由下面的状态码分支处理
  }

  if (!response.ok || body?.ok === false) {
    const message = body?.error?.message || `请求失败（${response.status}）`;
    throw new Error(message);
  }

  return body?.data ?? body;
}
