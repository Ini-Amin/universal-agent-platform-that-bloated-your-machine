// Address handling for the "add a surface" form.

/**
 * Accepts "example.com", "https://example.com/x", "/relative" and data: URLs.
 * Returns the address to open, or '' when it is not a usable one.
 */
export function normalizeToolUrl(raw) {
  const text = String(raw || '').trim();
  if (/^data:/i.test(text) || text.startsWith('/')) return text;
  if (!text || /\s/.test(text)) return '';
  let candidate = text;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) {
    // localhost and IP addresses are almost always plain http.
    const local = /^(localhost|\d{1,3}(\.\d{1,3}){3}|\[[0-9a-f:]+\])(:\d+)?(\/|$)/i.test(text);
    candidate = `${local ? 'http' : 'https'}://${text}`;
  }
  let u;
  try {
    u = new URL(candidate);
  } catch (_e) {
    return '';
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return '';
  // "google" or "hello" are typos, not addresses: a host needs a dot, or be localhost.
  if (!u.hostname.includes('.') && u.hostname !== 'localhost' && !u.hostname.startsWith('[')) return '';
  return candidate;
}
