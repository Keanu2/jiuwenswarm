/**
 * 跨设备任务的展示辅助：错误码读取与文案翻译。
 *
 * 供 TaskChatView 与侧边栏任务条目共用——对同一份 relay 状态的呈现必须
 * 一致（同一个错误码翻出同一条 i18n）。
 */

/** Read the server's error code off a rejected webRequest, for i18n lookup. */
export function errorCode(error: unknown): string {
  if (error && typeof error === 'object' && 'code' in error) {
    return String((error as { code?: unknown }).code ?? '');
  }
  return '';
}

/** 把错误码翻成 i18n 文案；未知码回退到通用文案（由调用方传入）。 */
export function taskErrorText(
  translate: (key: string, options?: Record<string, unknown>) => string,
  code: string | null | undefined,
  fallbackKey = 'taskRelay.errors.action',
): string {
  if (!code) return translate(fallbackKey);
  return translate(`taskRelay.errors.${code}`, { defaultValue: translate(fallbackKey) });
}
