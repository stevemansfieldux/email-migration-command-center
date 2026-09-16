// Task drawer. One task open at a time; every edit saves inline through the API and
// reflects back onto the board card behind the drawer without a reload.
let paneTaskId = null;
let paneTask = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const STATUS_VAR = { suggested: 'var(--dim)', open: 'var(--open)', doing: 'var(--doing)', blocked: 'var(--blocked)', done: 'var(--done)', dismissed: 'var(--dim)' };

function initials(name) { return (name || '?').split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase(); }
function avatarColor(name) { let h = 0; for (const c of name || '') h = (h * 31 + c.charCodeAt(0)) >>> 0; return `hsl(${h % 360} 45% 42%)`; }
function when(iso) { const d = new Date(iso); return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }); }
function md(s) {
  // Enough markdown for a comment: bold, italic, inline code, links, @mentions, line breaks.
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|\s)_([^_]+)_/g, '$1<em>$2</em>')
    .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
    .replace(/@(\w+)/g, '<strong>@$1</strong>');
}

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) { const t = await r.text().catch(() => ''); throw new Error(`${r.status} ${t.slice(0, 120)}`); }
  return r.status === 204 ? null : r.json();
}

// ---------- open / close ----------

async function openTask(id) {
  try { paneTask = await api('GET', `/api/tasks/${id}`); } catch (e) { alert(`Couldn't load task #${id}: ${e.message}`); return; }
  paneTaskId = id;
  renderPane(paneTask);
  showPaneTab('brief');
  $('task-pane').classList.add('open'); $('task-backdrop').classList.add('open');
  $('task-pane').setAttribute('aria-hidden', 'false');
  const u = new URL(location); u.searchParams.set('task', paneTask?.ref || id); history.replaceState(null, '', u);
}

function closeTask() {
  $('task-pane').classList.remove('open'); $('task-backdrop').classList.remove('open');
  $('task-pane').setAttribute('aria-hidden', 'true');
  paneTaskId = null; paneTask = null;
  const u = new URL(location); u.searchParams.delete('task'); history.replaceState(null, '', u);
}
document.addEventListener('keydown', e => { if (e.key === 'Escape' && paneTaskId) closeTask(); });

// ---------- render ----------

function renderPane(t) {
  $('pane-ref').textContent = t.ref || `#${t.id}`;
  $('pane-page').href = `/tasks/${t.ref || t.id}`;
  $('pane-title').textContent = t.title;
  paintStatus(t.status); paintPrio(t.priority); paintDue(t.due); paintOwner(t.owner || t.suggested_owner || 'unassigned');
  $('sel-status').value = t.status; $('sel-prio').value = t.priority; $('inp-due').value = t.due || ''; $('sel-owner').value = t.owner || 'unassigned';
  // suggestion bar
  const sb = $('pane-sugg'); sb.hidden = t.status !== 'suggested';
  if (!sb.hidden) { $('pane-sugg-for').textContent = t.suggested_owner || 'unassigned'; $('pane-sugg-dup').textContent = t.dup_of ? ` May duplicate #${t.dup_of}.` : ''; }
  // source
  const src = $('pane-source');
  src.innerHTML = t.source_meeting ? `<a href="/meetings/view/${esc(t.source_meeting)}">${esc(t.source_ref || t.source_meeting)}</a>` : esc(t.source_ref || t.source || '—');
  if (t.created_by) src.innerHTML += ` <span class="mono">· by ${esc(t.created_by)}</span>`;
  renderTags(t.tags || []);
  // context: quotes stay read-only above an editable body
  const parts = (t.detail || '').split('\n\n> ');
  const body = parts[0] || ''; const quotes = parts.slice(1);
  $('pane-quotes').innerHTML = quotes.map(q => `<div class="pq">${esc(q)}</div>`).join('');
  const d = $('pane-desc'); d.textContent = body; d.classList.add('clamp');
  requestAnimationFrame(() => { const tog = $('pane-desc-toggle'); tog.hidden = d.scrollHeight <= d.clientHeight + 2; tog.textContent = 'Show more'; });
  renderMilestones(t.milestones || []);
  renderComments(t.comments || []);
  $('pane-history').innerHTML = '<div class="muted">Loading…</div>';
}

function paintStatus(s) { $('chip-status-lbl').textContent = s; $('chip-status-dot').style.background = STATUS_VAR[s] || 'var(--dim)'; $('pane-dot').style.background = STATUS_VAR[s] || 'var(--dim)'; }
function paintPrio(p) { $('chip-prio-lbl').textContent = p; $('chip-prio').style.borderColor = p === 'high' ? 'var(--blocked)' : 'var(--line)'; }
function paintDue(d) { $('chip-due-lbl').textContent = d || '—'; }
function paintOwner(o) { $('chip-owner-lbl').textContent = o; const av = $('chip-owner-av'); av.textContent = o === 'unassigned' ? '?' : initials(o); av.style.background = o === 'unassigned' ? 'var(--dim)' : avatarColor(o); }

function renderTags(tags) {
  $('pane-tags').innerHTML = tags.map(t => `<span class="chip">#${esc(t)}<span class="rm" title="remove" onclick="paneRemoveTag('${esc(t)}')">×</span></span>`).join('')
    + `<form onsubmit="return paneAddTag(event)"><input type="text" id="pane-tag-input" placeholder="add tag" autocomplete="off"></form>`;
}
function renderMilestones(ms) {
  const done = ms.filter(m => m.done).length;
  $('pane-ms-count').textContent = ms.length ? `${done}/${ms.length}` : '';
  $('pane-milestones').innerHTML = ms.length ? ms.map(m => `<div class="mi ${m.done ? 'done' : ''}"><button type="button" class="tick" onclick="paneToggleMilestone(${m.id})" aria-label="toggle"></button><span class="t">${esc(m.text)}</span><button type="button" class="del" onclick="paneDeleteMilestone(${m.id})" aria-label="delete">×</button></div>`).join('') : '<div class="muted">No milestones yet.</div>';
}
function renderComments(cs) {
  $('pane-conv-n').textContent = cs.length;
  $('pane-comments').innerHTML = cs.length ? cs.map(c => `<div class="cm"><div class="who"><span class="av" style="background:${avatarColor(c.author)}">${initials(c.author)}</span><span>${esc(c.author)}</span><span>${when(c.created_at)}</span></div><div class="b">${md(c.body)}</div></div>`).join('') : '<div class="muted">No comments yet.</div>';
}
async function loadHistory() {
  try {
    const ev = await api('GET', `/api/tasks/${paneTaskId}/history`);
    $('pane-history').innerHTML = ev.length ? ev.map(e => `<div class="hi-row"><span class="w">${when(e.created_at)}</span><span><span class="k">${esc(e.kind)}</span>${esc(e.actor)}${e.detail ? ' — ' + esc(e.detail) : ''}</span></div>`).join('') : '<div class="muted">No history yet.</div>';
  } catch { $('pane-history').innerHTML = '<div class="muted">Couldn\'t load history.</div>'; }
}

function showPaneTab(name) {
  document.querySelectorAll('.ptabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.pbody section').forEach(s => s.hidden = s.dataset.tab !== name);
  $('pane-footer').hidden = name !== 'conv';
  if (name === 'hist') loadHistory();
}
function toggleDesc() { const d = $('pane-desc'), t = $('pane-desc-toggle'); d.classList.toggle('clamp'); t.textContent = d.classList.contains('clamp') ? 'Show more' : 'Show less'; }

// ---------- reflect onto the board card ----------

const TODAY = new Date().toISOString().slice(0, 10);
function niceDate(v) { const d = new Date(v + 'T00:00:00'); return isNaN(d) ? v : `${d.getDate()} ${d.toLocaleString('en-GB', { month: 'short' })}`; }
function reflectToCard(t) {
  const card = document.querySelector(`.task[data-id="${t.id}"]`);
  if (!card) return;
  card.querySelector('.title a').textContent = t.title;
  const col = document.querySelector(`.col[data-status="${t.status}"]`);
  if (col && card.parentElement !== col) { col.insertBefore(card, col.querySelector('.empty')); if (typeof recount === 'function') recount(); }
  card.dataset.owner = t.owner || '';
  const meta = card.querySelector('.meta');
  if (meta) {
    const own = meta.querySelector('.own'); const name = t.owner || 'unassigned';
    if (own) { own.querySelector('.avatar').textContent = name[0].toUpperCase(); own.lastChild.textContent = name; }
    let hi = meta.querySelector('.hi'); if (t.priority === 'high' && !hi) { hi = document.createElement('span'); hi.className = 'hi'; hi.textContent = 'High'; own.after(hi); } else if (t.priority !== 'high' && hi) hi.remove();
    let due = meta.querySelector('.due'); if (t.due) { if (!due) { due = document.createElement('span'); (hi || own).after(due); } const late = t.due < TODAY && t.status !== 'done'; due.className = 'due' + (late ? ' late' : ''); due.innerHTML = (late ? 'Overdue' : 'Due') + '<small></small>'; due.querySelector('small').textContent = niceDate(t.due); } else if (due) due.remove();
    let prog = meta.querySelector('.prog'); if (t.milestones_total) { if (!prog) { prog = document.createElement('span'); prog.className = 'prog'; meta.insertBefore(prog, meta.querySelector('.cc')); } prog.innerHTML = '<b></b> subtasks'; prog.querySelector('b').textContent = `${t.milestones_done}/${t.milestones_total}`; } else if (prog) prog.remove();
    const cc = meta.querySelector('.cc'); if (cc) { const n = t.comment_count ?? (t.comments || []).length; cc.innerHTML = '<b class="n"></b> ' + (n === 1 ? 'comment' : 'comments'); cc.querySelector('.n').textContent = n; cc.hidden = !n; }
    meta.querySelectorAll('.chip').forEach(c => c.remove());
    (t.tags || []).forEach(tg => { const a = document.createElement('a'); a.className = 'chip'; a.href = `/?tag=${tg}`; a.textContent = `#${tg}`; meta.appendChild(a); });
  }
  if (typeof applyOwnerFilter === 'function') { const on = document.querySelector('#owner-seg button.on'); applyOwnerFilter(on ? on.dataset.owner : ''); }
}

// ---------- saves ----------

async function paneSave(field, value) {
  try {
    paneTask = await api('PATCH', `/api/tasks/${paneTaskId}`, { [field]: value });
    if (field === 'status') { paintStatus(paneTask.status); $('pane-sugg').hidden = paneTask.status !== 'suggested'; if (!['open','doing','blocked','done'].includes(paneTask.status)) { document.querySelector(`.task[data-id="${paneTaskId}"]`)?.remove(); if (typeof recount === 'function') recount(); } }
    if (field === 'priority') paintPrio(paneTask.priority);
    if (field === 'due') paintDue(paneTask.due);
    if (field === 'owner') paintOwner(paneTask.owner);
    reflectToCard(paneTask);
  } catch (e) { alert(`Save failed: ${e.message}`); }
}
function paneSaveTitle() { const v = $('pane-title').textContent.trim(); if (v && v !== paneTask.title) paneSave('title', v); else $('pane-title').textContent = paneTask.title; }
function paneSaveDesc() { const v = $('pane-desc').textContent.trim(); const cur = (paneTask.detail || '').split('\n\n> ')[0].trim(); if (v !== cur) paneSave('detail', v); }

async function paneAccept() { try { paneTask = await api('POST', `/api/suggestions/${paneTaskId}/accept`, {}); location.reload(); } catch (e) { alert(e.message); } }
async function paneDismiss() { try { await api('POST', `/api/suggestions/${paneTaskId}/dismiss`); location.reload(); } catch (e) { alert(e.message); } }
async function paneArchive() { if (!confirm('Archive this task? It leaves the board but can be restored.')) return; try { await api('DELETE', `/api/tasks/${paneTaskId}`); document.querySelector(`.task[data-id="${paneTaskId}"]`)?.remove(); if (typeof recount === 'function') recount(); closeTask(); } catch (e) { alert(e.message); } }

async function paneAddTag(ev) { ev.preventDefault(); const v = $('pane-tag-input').value.trim(); if (!v) return false; try { const r = await api('POST', `/api/tasks/${paneTaskId}/tags`, { tag: v }); paneTask.tags = r.tags; renderTags(r.tags); reflectToCard(paneTask); } catch (e) { alert(e.message); } return false; }
async function paneRemoveTag(tag) { try { const r = await api('DELETE', `/api/tasks/${paneTaskId}/tags/${encodeURIComponent(tag)}`); paneTask.tags = r.tags; renderTags(r.tags); reflectToCard(paneTask); } catch (e) { alert(e.message); } }

async function refreshMilestones() { paneTask = await api('GET', `/api/tasks/${paneTaskId}`); renderMilestones(paneTask.milestones); reflectToCard(paneTask); }
async function paneAddMilestone(ev) { ev.preventDefault(); const i = $('pane-ms-input'); const v = i.value.trim(); if (!v) return false; try { await api('POST', `/api/tasks/${paneTaskId}/milestones`, { text: v }); i.value = ''; await refreshMilestones(); } catch (e) { alert(e.message); } return false; }
async function paneToggleMilestone(id) { try { await api('POST', `/api/milestones/${id}/toggle`); await refreshMilestones(); } catch (e) { alert(e.message); } }
async function paneDeleteMilestone(id) { try { await api('DELETE', `/api/milestones/${id}`); await refreshMilestones(); } catch (e) { alert(e.message); } }

async function paneAddComment(ev) {
  ev.preventDefault(); const ta = $('pane-cbody'); const v = ta.value.trim(); if (!v) return false;
  try { await api('POST', `/api/tasks/${paneTaskId}/comments`, { body: v }); ta.value = ''; paneTask = await api('GET', `/api/tasks/${paneTaskId}`); renderComments(paneTask.comments); reflectToCard(paneTask); $('pane-comments').scrollIntoView({ block: 'end' }); }
  catch (e) { alert(`Comment failed: ${e.message}`); }
  return false;
}

// Cards open the drawer; the title link still works as a plain page (middle-click, etc.)
document.addEventListener('click', e => {
  const card = e.target.closest('.task[data-id]');
  if (!card || e.target.closest('a, button, select, form')) return;
  openTask(Number(card.dataset.id));
});
document.addEventListener('click', e => {
  const a = e.target.closest('.task .title a');
  if (a && !e.metaKey && !e.ctrlKey && e.button === 0) { e.preventDefault(); openTask(Number(a.closest('.task').dataset.id)); }
});
