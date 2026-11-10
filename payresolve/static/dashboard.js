document.querySelectorAll('[data-example]').forEach(button => {
  button.addEventListener('click', () => {
    const field = document.getElementById('message');
    field.value = button.dataset.example;
    field.focus();
    field.scrollIntoView({behavior: 'smooth', block: 'center'});
  });
});
document.querySelectorAll('form[data-wait]').forEach(form => {
  form.addEventListener('submit', () => {
    form.setAttribute('aria-busy', 'true');
    form.querySelectorAll('button').forEach(button => { button.disabled = true; });
    const status = form.querySelector('.working');
    status.textContent = form.dataset.wait;
    status.hidden = false;
  });
});
window.addEventListener('pageshow', () => {
  document.querySelectorAll('form[aria-busy]').forEach(form => {
    form.removeAttribute('aria-busy');
    form.querySelectorAll('button').forEach(button => { button.disabled = false; });
    form.querySelector('.working').hidden = true;
  });
});

// Keep the chat page present while the server works so observed activity can update.
document.querySelectorAll('form[data-case-chat]').forEach(form => {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const body = new FormData(form);
    const field = form.querySelector('textarea');
    const status = form.querySelector('.working');
    field.disabled = true;
    try {
      const response = await fetch(form.action, {method: 'POST', body, credentials: 'same-origin'});
      if (response.ok && response.redirected && new URL(response.url).origin === location.origin) {
        location.assign(response.url);
        return;
      }
      status.textContent = response.status === 409 ? 'This case changed. Reload the latest case before sending again.' :
        'The message could not be completed. Reload the case to check its saved state.';
    } catch (_) {
      status.textContent = 'Connection interrupted. Your message may have been saved; reload the case before trying again.';
    }
    form.removeAttribute('aria-busy');
    field.disabled = false;
    form.querySelectorAll('button').forEach(button => { button.disabled = false; });
  });
});
const conversationFeed = document.querySelector('.conversation-feed');
if (conversationFeed) conversationFeed.scrollTop = conversationFeed.scrollHeight;

// Poll local state only. The server worker owns provider calls and durable budgets.
const liveCase = document.querySelector('[data-case-state-url]');
if (liveCase) {
  const jobState = jobs => JSON.stringify(jobs.map(job =>
    [job.kind, job.status, job.attempts, job.max_attempts, job.last_error]));
  const initialJobs = jobState(JSON.parse(liveCase.dataset.caseJobs));
  const refresh = async () => {
    if (document.visibilityState !== 'visible') return;
    try {
      const response = await fetch(liveCase.dataset.caseStateUrl, {cache: 'no-store'});
      if (!response.ok) return;
      const state = await response.json();
      const activeChat = document.querySelector('form[data-case-chat][aria-busy="true"] .working');
      if (activeChat && state.activity) activeChat.textContent = state.activity;
      if (state.version !== Number(liveCase.dataset.caseVersion) || jobState(state.jobs) !== initialJobs) {
        const editing = Array.from(document.querySelectorAll('textarea')).some(field => field.value.trim()) ||
          document.querySelector('form[aria-busy="true"]') || document.querySelector('input[type="checkbox"]:checked');
        if (!editing) window.location.reload();
        else document.getElementById('case-update-notice').hidden = false;
      }
    } catch (_) {
      // A local restart may briefly disconnect the page. Poll again without POSTing.
    }
  };
  setInterval(refresh, 3000);
  refresh();
}
