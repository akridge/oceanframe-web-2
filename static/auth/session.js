/**
 * Shared by every page: who is signed in, and what to do when the session ends.
 *
 * `authFetch` is a drop-in for fetch that sends the page to the sign-in screen on
 * a 401 — a session that expires mid-task lands the user back where they were
 * once they sign in again, instead of failing every request with a vague error.
 */

let mePromise = null;

/** {user, auth_enabled, roles} — fetched once per page. */
export function whoami() {
  if (!mePromise) {
    mePromise = fetch('/api/auth/me', { credentials: 'same-origin' })
      .then(res => (res.ok ? res.json() : null))
      .catch(() => null);
  }
  return mePromise;
}

export function goToLogin() {
  const here = window.location.pathname + window.location.search;
  window.location.assign(`/login?next=${encodeURIComponent(here)}`);
}

export async function authFetch(url, options = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...options });
  if (res.status === 401) {
    goToLogin();
    // Never resolves: the page is navigating away, and callers should not
    // flash an error in the meantime.
    return new Promise(() => {});
  }
  return res;
}

/** JSON request that throws the server's `detail` on failure. */
export async function apiJson(url, body, method = 'POST') {
  const res = await authFetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = null;
  try { payload = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error((payload && payload.detail) || `${res.status} ${res.statusText}`);
  return payload;
}

export function roleAtLeast(user, role) {
  const order = ['viewer', 'curator', 'lead', 'admin'];
  return !!user && order.indexOf(user.role) >= order.indexOf(role);
}
