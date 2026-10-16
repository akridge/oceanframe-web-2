/** Own profile and password. */
import { apiJson, whoami } from './session.js';

const profileForm = document.getElementById('profile-form');
const passwordForm = document.getElementById('password-form');

function say(id, text, isError = false) {
  const node = document.getElementById(id);
  node.textContent = text;
  node.classList.toggle('is-error', isError);
}

async function init() {
  const me = await whoami();
  if (!me || !me.auth_enabled) {
    document.getElementById('account-off').hidden = false;
    document.getElementById('profile-card').hidden = true;
    document.getElementById('password-card').hidden = true;
    return;
  }
  const u = me.user;
  document.getElementById('profile-who').textContent = `${u.username} · ${u.role}`;
  profileForm.display_name.value = u.display_name || '';
  profileForm.initials.value = u.initials || '';
  profileForm.email.value = u.email || '';
}

profileForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await apiJson('/api/auth/profile', {
      display_name: profileForm.display_name.value,
      initials: profileForm.initials.value,
      email: profileForm.email.value,
    }, 'PUT');
    say('profile-status', 'Saved.');
  } catch (err) {
    say('profile-status', err.message, true);
  }
});

passwordForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  try {
    await apiJson('/api/auth/password', {
      old_password: passwordForm.old_password.value,
      new_password: passwordForm.new_password.value,
    }, 'PUT');
    passwordForm.reset();
    say('password-status', 'Password changed.');
  } catch (err) {
    say('password-status', err.message, true);
  }
});

init();
