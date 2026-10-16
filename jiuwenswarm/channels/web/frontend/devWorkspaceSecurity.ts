/** Vite exposes workspace files and debug controls, so it remains local only. */
export function isLocalDevelopmentRequest(peer: string | undefined, origin?: string): boolean {
  function loopback(host: string): boolean {
    const normalized = host
      .replace(/^\[/, '')
      .replace(/\]$/, '')
      .replace(/^::ffff:/, '');
    return normalized === '::1' || normalized === 'localhost' || /^127(?:\.\d{1,3}){3}$/.test(normalized);
  }
  if (!peer || !loopback(peer)) return false;
  if (!origin) return true;
  try {
    const url = new URL(origin);
    return (url.protocol === 'http:' || url.protocol === 'https:') && loopback(url.hostname);
  } catch {
    return false;
  }
}
