/** Sign-in and (when the deployment allows it) sign-up. */
const form = document.getElementById('login-form');
const error = document.getElementById('login-error');
const submit = document.getElementById('login-submit');

function safeNext(target) {
  // Same-site paths only; the server checks this too.
  if (!target || !target.startsWith('/') || target.startsWith('//') || target.includes('\\')) return '/';
  // A shared /curation#project=… link reaches us through the server's redirect, which cannot see the
  // fragment; the browser keeps it on this page's URL instead, so carry it on.
  const hash = window.location.hash;
  if (hash && hash !== '#signup' && !target.includes('#')) return target + hash;
  return target;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  error.hidden = true;
  submit.disabled = true;
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: form.username.value.trim(),
        password: form.password.value,
      }),
    });
    if (!res.ok) {
      let detail = 'Sign-in failed';
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    window.location.assign(safeNext(form.dataset.next));
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
    form.password.select();
  } finally {
    submit.disabled = false;
  }
});

// ── Sign-up ───────────────────────────────────────────────────────────────────
const signupForm = document.getElementById('signup-form');
if (signupForm) {
  const loginCard = document.getElementById('login-card');
  const signupCard = document.getElementById('signup-card');
  const showSignup = (on) => {
    loginCard.hidden = on;
    signupCard.hidden = !on;
    (on ? signupForm.username : form.username).focus();
  };
  document.getElementById('show-signup').addEventListener('click', (e) => { e.preventDefault(); showSignup(true); });
  document.getElementById('show-login').addEventListener('click', (e) => { e.preventDefault(); showSignup(false); });
  if (window.location.hash === '#signup') showSignup(true);

  const signupError = document.getElementById('signup-error');
  const signupSubmit = document.getElementById('signup-submit');
  signupForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    signupError.hidden = true;
    if (signupForm.password.value !== document.getElementById('su-confirm').value) {
      signupError.textContent = 'The two passwords do not match.';
      signupError.hidden = false;
      return;
    }
    signupSubmit.disabled = true;
    try {
      const res = await fetch('/api/auth/signup', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: signupForm.username.value.trim(),
          display_name: signupForm.display_name.value.trim(),
          email: signupForm.email.value.trim() || null,
          password: signupForm.password.value,
        }),
      });
      if (!res.ok) {
        let detail = 'Could not create the account';
        try {
          const body = await res.json();
          detail = typeof body.detail === 'string' ? body.detail : (body.detail?.[0]?.msg || detail);
        } catch { /* not JSON */ }
        throw new Error(detail);
      }
      // Signed in already: the server set the session cookie.
      window.location.assign(safeNext(signupForm.dataset.next));
    } catch (err) {
      signupError.textContent = err.message;
      signupError.hidden = false;
    } finally {
      signupSubmit.disabled = false;
    }
  });
}
