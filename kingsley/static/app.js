/* K1NG5L3Y console — vanilla JS, no build step, works from disk or Pages. */
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const params = new URLSearchParams(location.search);
const savedApi = localStorage.getItem('kingsley.api') || '';
const pagesHost = location.hostname.endsWith('github.io');
let API = (params.get('api') || savedApi || (pagesHost ? 'http://127.0.0.1:3000' : '')).replace(/\/+$/, '');

const state = {
  providers: [],
  google: {},
  routes: {},
  taskKinds: [],
  interests: [],
  online: false,
  listening: false,
  recognition: null,
  stream: null,
};

/* ---------------------------------------------------------------- helpers */

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function lite(text) {
  return esc(text)
    .replace(/```([\s\S]*?)```/g, (_m, code) => `<pre>${code}</pre>`)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
}

function clockTime(ts) {
  const date = new Date((ts || 0) * 1000);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function relTime(ts) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - (ts || 0)));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function toast(message, isError) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.toggle('err', Boolean(isError));
  node.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => node.classList.remove('show'), 4200);
}

async function api(path, options = {}) {
  const init = { method: options.method || 'GET', headers: { 'Content-Type': 'application/json' } };
  if (options.body !== undefined) init.body = JSON.stringify(options.body);
  let response;
  try {
    response = await fetch(API + path, init);
  } catch (error) {
    throw new Error(`backend unreachable at ${API || location.origin} — start it with: python -m kingsley`);
  }
  const raw = await response.text();
  let data = {};
  try {
    data = raw ? JSON.parse(raw) : {};
  } catch (_error) {
    data = { error: raw.slice(0, 300) };
  }
  if (!response.ok) throw new Error(data.detail || data.error || `HTTP ${response.status}`);
  return data;
}

function switchView(name) {
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.view === name));
  $$('.view').forEach((view) => view.classList.toggle('active', view.id === `view-${name}`));
  if (name === 'sensors') refreshDiagnostics();
  if (name === 'repo') loadTree();
}

/* ------------------------------------------------------------------- boot */

async function boot() {
  tickClock();
  setInterval(tickClock, 1000);
  $$('.nav-item').forEach((item) => item.addEventListener('click', () => switchView(item.dataset.view)));
  bindConsole();
  bindTasks();
  bindIntel();
  bindRepo();
  bindVision();
  bindAudio();
  bindLink();

  await loadState();
  loadChat();
  loadTasks();
  loadApprovals();
  loadTranscripts();
  loadIntelArchive();
  connectStream();
  refreshDiagnostics();
  setInterval(() => {
    if (document.visibilityState === 'visible') refreshDiagnostics();
  }, 4000);
}

function tickClock() {
  $('#clock').textContent = new Date().toLocaleTimeString([], { hour12: false });
}

async function loadState() {
  try {
    const data = await api('/api/state');
    state.online = true;
    state.providers = data.providers || [];
    state.google = data.google || {};
    state.routes = data.routes || {};
    state.taskKinds = data.task_kinds || [];
    state.interests = data.interests || [];
    setChip('#linkDot', 'ok', `LINK ${API || 'local'}`);
    renderProviderSelect();
    renderLink();
    fillInterests();
  } catch (error) {
    state.online = false;
    setChip('#linkDot', 'bad', 'LINK offline');
    toast(error.message, true);
  }
}

function setChip(selector, level, text) {
  const dot = $(selector);
  if (dot) dot.className = `dot ${level}`;
  if (selector === '#linkDot') $('#linkText').textContent = text;
}

function renderProviderSelect() {
  const select = $('#providerSelect');
  const current = select.value;
  const options = ['<option value="">auto (task router)</option>'];
  state.providers.forEach((provider) => {
    options.push(
      `<option value="${provider.slug}"${provider.configured ? '' : ' disabled'}>${esc(provider.label)}${provider.configured ? '' : ' — no key'}</option>`,
    );
  });
  select.innerHTML = options.join('');
  select.value = current;
  const usable = state.providers.filter((provider) => provider.configured);
  $('#consoleHint').innerHTML = usable.length
    ? `Router targets: ${usable.map((provider) => `<b>${esc(provider.label)}</b>`).join(', ')}. Task selector decides which one answers.`
    : 'No model keys detected. Add one under <b>LINK</b>, or run FreeLLMAPI on :3001 for free routing.';
  const model = $('#modelText');
  model.textContent = usable.length ? usable[0].label.toLowerCase() : 'none';
}

function renderLink() {
  const google = state.google || {};
  $('#googleText').textContent = google.connected ? 'linked' : google.credentials_configured ? 'ready' : 'no key';
  $('#googleState').innerHTML = google.connected
    ? `<span class="set">connected</span> as ${esc(google.email || 'account')}`
    : google.credentials_configured
      ? 'credentials found — click connect to authorise Gmail + Calendar'
      : 'not configured — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, restart the backend';
  const env = state.providers.map(
    (provider) =>
      `<li><b>${esc(provider.label)}</b><span class="${provider.configured ? 'set' : 'unset'}">${provider.configured ? 'set' : 'unset'}</span></li>`,
  );
  env.push(
    `<li><b>Google OAuth</b><span class="${google.credentials_configured ? 'set' : 'unset'}">${google.credentials_configured ? 'set' : 'unset'}</span></li>`,
  );
  env.push('<li><b>Workspace</b><span class="set">' + esc(shortPath(location.pathname === '/' ? 'repo root' : '')) + '</span></li>');
  $('#envList').innerHTML = env.join('');
}

function shortPath(value) {
  return value || 'repo root';
}

function fillInterests() {
  const input = $('#interestsInput');
  if (input && !input.value && state.interests.length) input.value = state.interests.join(', ');
}

/* ---------------------------------------------------------------- console */

function bindConsole() {
  $('#sendBtn').addEventListener('click', sendChat);
  $('#chatInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendChat();
    }
  });
}

function appendMessage(role, text, meta) {
  const log = $('#chatLog');
  const node = document.createElement('div');
  node.className = `msg ${role}`;
  const label = meta || (role === 'user' ? 'you' : role === 'assistant' ? 'kingsley' : 'system');
  node.innerHTML = `<div class="meta">${esc(label)} · ${clockTime(Date.now() / 1000)}</div><div class="body">${lite(text)}</div>`;
  log.appendChild(node);
  log.scrollTop = log.scrollHeight;
  return node;
}

async function loadChat() {
  try {
    const data = await api('/api/chat?limit=30');
    (data.messages || []).forEach((message) => {
      appendMessage(message.role === 'assistant' ? 'assistant' : 'user', message.text, message.provider || '');
    });
  } catch (_error) {
    /* offline: console still usable once backend appears */
  }
}

async function sendChat() {
  const input = $('#chatInput');
  const message = input.value.trim();
  if (!message) return;
  input.value = '';
  appendMessage('user', message);
  const pending = appendMessage('system', 'routing…');
  const payload = {
    message,
    task: $('#taskSelect').value,
    provider: $('#providerSelect').value || null,
  };
  try {
    const result = await api('/api/chat', { method: 'POST', body: payload });
    pending.remove();
    if (result.text) {
      appendMessage('assistant', result.text, `${result.provider_label || result.provider} · ${result.model} · ${result.task}`);
      $('#modelText').textContent = (result.provider_label || result.provider || '').toLowerCase();
    } else {
      const attempts = (result.attempts || []).map((item) => `${item.provider}: ${item.error}`).join(' | ');
      appendMessage('system', `no provider could answer.\n${attempts}`);
    }
  } catch (error) {
    pending.remove();
    appendMessage('system', `error: ${error.message}`);
  }
}

/* ------------------------------------------------------------------ tasks */

function bindTasks() {
  $('#addTaskBtn').addEventListener('click', async () => {
    const title = $('#taskTitle').value.trim();
    if (!title) return toast('task title is required', true);
    try {
      await api('/api/tasks', {
        method: 'POST',
        body: { title, due: $('#taskDue').value, priority: $('#taskPriority').value },
      });
      $('#taskTitle').value = '';
      $('#taskDue').value = '';
      toast('task added');
      loadTasks();
    } catch (error) {
      toast(error.message, true);
    }
  });

  $('#organiseBtn').addEventListener('click', async () => {
    toast('asking the router to re-order the board…');
    try {
      const result = await api('/api/tasks/organize', { method: 'POST', body: {} });
      $('#taskHint').textContent = result.raw || 'board re-ordered';
      loadTasks();
    } catch (error) {
      toast(error.message, true);
    }
  });

  $('#syncCalBtn').addEventListener('click', async () => {
    try {
      const result = await api('/api/calendar/sync', { method: 'POST', body: {} });
      toast(`calendar: ${result.synced.length} scheduled, ${result.skipped.length} skipped`);
      loadTasks();
    } catch (error) {
      toast(error.message, true);
    }
  });

  $('#triageBtn').addEventListener('click', async () => {
    toast('reading unread mail…');
    try {
      const result = await api('/api/gmail/triage', { method: 'POST', body: { limit: 12 } });
      toast(`triaged ${result.emails} emails → ${result.created.length} tasks`);
      loadTasks();
    } catch (error) {
      toast(error.message, true);
    }
  });
}

async function loadTasks() {
  try {
    const data = await api('/api/tasks');
    const tasks = data.tasks || [];
    $('#taskList').innerHTML = tasks.length
      ? tasks
          .map((task) => {
            const done = task.status === 'done';
            const due = task.due ? `<span class="t-meta">due ${esc(task.due)}</span>` : '';
            const cal = task.calendar_event_id ? '<span class="badge cal">on cal</span>' : '';
            return `<div class="task ${done ? 'done' : ''}" data-id="${task.id}">
              <button class="t-check" data-act="toggle">${done ? '◉' : '○'}</button>
              <div><div class="t-title">${esc(task.title)}</div>${task.detail ? `<div class="t-detail">${esc(task.detail)}</div>` : ''}</div>
              <span class="badge ${esc(task.priority)}">${esc(task.priority)}</span>
              ${due}
              ${cal}
              <button class="btn ghost" data-act="delete">DEL</button>
            </div>`;
          })
          .join('')
      : '<p class="hint">no tasks yet — add one, triage Gmail, or run the interest scout.</p>';

    $('#taskList').querySelectorAll('.task').forEach((row) => {
      const id = row.dataset.id;
      row.querySelector('[data-act="toggle"]').addEventListener('click', async () => {
        const done = row.classList.contains('done');
        await api(`/api/tasks/${id}`, { method: 'PATCH', body: { status: done ? 'open' : 'done' } });
        loadTasks();
      });
      row.querySelector('[data-act="delete"]').addEventListener('click', async () => {
        await api(`/api/tasks/${id}`, { method: 'DELETE' });
        loadTasks();
      });
    });
    const open = tasks.filter((task) => task.status === 'open').length;
    $('#statTasks').textContent = open;
  } catch (_error) {
    /* backend offline */
  }
}

/* ------------------------------------------------------------------ intel */

function bindIntel() {
  $('#scoutBtn').addEventListener('click', async () => {
    const raw = $('#interestsInput').value.trim();
    const topics = raw ? raw.split(',').map((item) => item.trim()).filter(Boolean) : null;
    $('#intelOut').textContent = 'scouting… searching your interests and asking the news router to brief you.';
    try {
      const result = await api('/api/scout', { method: 'POST', body: { topics } });
      $('#intelOut').textContent = result.briefing || '(no briefing returned)';
      $('#sourcesOut').innerHTML = (result.sources || [])
        .map((source) => `<li><a href="${esc(source.url)}" target="_blank" rel="noreferrer">${esc(source.title || source.url)}</a></li>`)
        .join('');
      toast(`briefing ready${result.created && result.created.length ? ` · ${result.created.length} focus tasks` : ''}`);
      loadIntelArchive();
      loadTasks();
    } catch (error) {
      $('#intelOut').textContent = `scout failed: ${error.message}`;
      toast(error.message, true);
    }
  });
}

async function loadIntelArchive() {
  try {
    const data = await api('/api/intel?limit=6');
    $('#intelArchive').innerHTML = (data.intel || [])
      .map((entry) => `<li><b>${esc(entry.topic)}</b><br><span class="dim">${relTime(entry.created_at)}</span></li>`)
      .join('') || '<li class="dim">empty</li>';
  } catch (_error) {
    /* offline */
  }
}

/* ------------------------------------------------------------------- repo */

function bindRepo() {
  $('#gitStatusBtn').addEventListener('click', () => runGit('status'));
  $('#gitDiffBtn').addEventListener('click', () => runGit('diff'));
  $('#gitLogBtn').addEventListener('click', () => runGit('log'));
  $('#repoAgentBtn').addEventListener('click', proposePatch);
}

async function runGit(cmd) {
  $('#gitOut').textContent = `$ git ${cmd} …`;
  try {
    const result = await api(`/api/repo/git?cmd=${cmd}`);
    $('#gitOut').textContent = `$ ${result.command}\n${result.output || '(no output)'}`;
  } catch (error) {
    $('#gitOut').textContent = `git failed: ${error.message}`;
  }
}

async function loadTree() {
  try {
    const data = await api('/api/repo/tree?depth=2');
    const entries = data.entries || [];
    $('#repoTree').innerHTML = entries
      .filter((entry) => entry.type === 'dir' || /\.(py|md|ts|tsx|js|json|toml|yml|css|html)$/.test(entry.path))
      .slice(0, 220)
      .map((entry) =>
        entry.type === 'dir'
          ? `<div class="tree-item dir">▸ ${esc(entry.path)}</div>`
          : `<button class="tree-item" data-path="${esc(entry.path)}"><span>${esc(entry.path)}</span><span class="sz">${entry.size}b</span></button>`,
      )
      .join('');
    $('#repoTree').querySelectorAll('button[data-path]').forEach((node) => {
      node.addEventListener('click', () => openFile(node.dataset.path));
    });
  } catch (error) {
    $('#repoTree').innerHTML = `<div class="tree-item dir">${esc(error.message)}</div>`;
  }
}

async function openFile(path) {
  $('#filePath').textContent = path;
  try {
    const data = await api(`/api/repo/file?path=${encodeURIComponent(path)}`);
    $('#fileView').textContent = data.text;
  } catch (error) {
    $('#fileView').textContent = `cannot read ${path}: ${error.message}`;
  }
}

async function proposePatch() {
  const instruction = $('#repoInstruction').value.trim();
  if (!instruction) return toast('describe the change first', true);
  const button = $('#repoAgentBtn');
  button.disabled = true;
  button.textContent = 'THINKING…';
  try {
    const result = await api('/api/repo/agent', { method: 'POST', body: { instruction } });
    toast(result.approvals.length ? `${result.approvals.length} patch(es) waiting for approval` : 'no edits proposed');
    if (result.summary) $('#gitOut').textContent = result.summary;
    loadApprovals();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = 'PROPOSE';
  }
}

async function loadApprovals() {
  try {
    const data = await api('/api/approvals');
    const items = data.approvals || [];
    $('#approvals').innerHTML = items.length
      ? items
          .map(
            (item) => `<div class="approval" data-id="${item.id}">
              <div class="a-head">
                <span class="a-path">${esc(item.path)}</span>
                <span>
                  <button class="btn primary" data-act="approve">APPLY</button>
                  <button class="btn danger" data-act="reject">DROP</button>
                </span>
              </div>
              <div class="hint">${esc(item.summary || '')}</div>
              <pre>${esc(item.diff || '')}</pre>
            </div>`,
          )
          .join('')
      : '<p class="hint">no pending patches. instruct the agent above to propose one.</p>';
    $('#approvals').querySelectorAll('.approval').forEach((node) => {
      const id = node.dataset.id;
      node.querySelector('[data-act="approve"]').addEventListener('click', async () => {
        try {
          const result = await api(`/api/approvals/${id}/approve`, { method: 'POST', body: {} });
          toast(`applied ${result.path}`);
          loadApprovals();
          loadTree();
        } catch (error) {
          toast(error.message, true);
        }
      });
      node.querySelector('[data-act="reject"]').addEventListener('click', async () => {
        await api(`/api/approvals/${id}/reject`, { method: 'POST', body: {} });
        loadApprovals();
      });
    });
    $('#statApprovals').textContent = items.length;
  } catch (_error) {
    /* offline */
  }
}

/* ----------------------------------------------------------------- vision */

function bindVision() {
  $('#shareBtn').addEventListener('click', shareScreen);
  $('#analyseBtn').addEventListener('click', analyseFrame);
}

async function shareScreen() {
  try {
    const stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 5 }, audio: false });
    const video = $('#visionVideo');
    video.srcObject = stream;
    video.style.display = 'block';
    $('#visionEmpty').style.display = 'none';
    stream.getVideoTracks()[0].addEventListener('ended', () => {
      video.style.display = 'none';
      $('#visionEmpty').style.display = 'block';
    });
    toast('screen linked — analyse when ready');
  } catch (error) {
    toast(`screen share cancelled: ${error.message}`, true);
  }
}

async function analyseFrame() {
  const video = $('#visionVideo');
  if (!video.videoWidth) return toast('share a screen first', true);
  const scale = Math.min(1, 1600 / video.videoWidth);
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
  const dataUrl = canvas.toDataURL('image/jpeg', 0.72);
  $('#visionOut').textContent = 'analysing frame…';
  try {
    const result = await api('/api/vision', {
      method: 'POST',
      body: { image: dataUrl, question: $('#visionQuestion').value.trim() || undefined },
    });
    $('#visionOut').textContent = result.text || `vision failed: ${(result.attempts || []).map((a) => `${a.provider}: ${a.error}`).join(' | ')}`;
  } catch (error) {
    $('#visionOut').textContent = `vision failed: ${error.message}`;
    toast(error.message, true);
  }
}

/* ------------------------------------------------------------------ audio */

function bindAudio() {
  $('#listenBtn').addEventListener('click', toggleListening);
  $('#manualSend').addEventListener('click', () => {
    const text = $('#manualText').value.trim();
    if (text) {
      $('#manualText').value = '';
      ingest(text);
    }
  });
}

function toggleListening() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    toast('this browser has no speech recognition — use Chrome or Edge', true);
    return;
  }
  if (state.listening) {
    state.listening = false;
    state.recognition && state.recognition.stop();
    $('#listenBtn').textContent = 'START LISTENING';
    $('#listenText').textContent = 'idle';
    $('#listenState').textContent = 'mic idle';
    setChip('#linkDot', 'ok', 'LINK');
    return;
  }

  const recognition = new Recognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = 'en-US';
  recognition.onresult = (event) => {
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      if (event.results[index].isFinal) {
        const text = event.results[index][0].transcript.trim();
        if (text.length > 2) ingest(text);
      }
    }
  };
  recognition.onerror = (event) => {
    $('#listenState').textContent = `mic error: ${event.error}`;
    toast(`microphone: ${event.error}`, true);
  };
  recognition.onend = () => {
    if (state.listening) {
      try {
        recognition.start();
      } catch (_error) {
        /* restart race, ignore */
      }
    }
  };
  state.recognition = recognition;
  state.listening = true;
  recognition.start();
  $('#listenBtn').textContent = 'STOP LISTENING';
  $('#listenText').textContent = 'live';
  $('#listenState').textContent = 'listening — say “Kingsley …”';
}

async function ingest(text) {
  try {
    await api('/api/listen', { method: 'POST', body: { text, kind: 'speech' } });
    appendTranscript(text, /kingsley/i.test(text));
  } catch (error) {
    toast(error.message, true);
  }
}

function appendTranscript(text, wake) {
  const node = document.createElement('div');
  node.className = `line${wake ? ' wake' : ''}`;
  node.innerHTML = `<time>${clockTime(Date.now() / 1000)}</time><span>${esc(text)}</span>`;
  $('#transcriptLog').prepend(node);
}

async function loadTranscripts() {
  try {
    const data = await api('/api/transcripts?limit=40');
    (data.transcripts || []).reverse().forEach((row) => appendTranscript(row.text, /kingsley/i.test(row.text)));
    $('#statTranscripts').textContent = (data.transcripts || []).length;
  } catch (_error) {
    /* offline */
  }
}

/* ---------------------------------------------------------------- sensors */

async function refreshDiagnostics() {
  try {
    const data = await api('/api/diagnostics');
    renderSensors(data);
    renderTelemetry(data);
    renderMatrix(data.providers || {});
    $('#statUptime').textContent = formatUptime(data.uptime_seconds);
    $('#statTranscripts').textContent = data.counters.transcripts;
    $('#statApprovals').textContent = data.counters.approvals_pending;
    $('#statTasks').textContent = data.counters.tasks_open;
    $('#sensorStamp').textContent = `updated ${new Date().toLocaleTimeString()}`;
    setChip('#linkDot', 'ok', 'LINK live');
  } catch (_error) {
    setChip('#linkDot', 'bad', 'LINK offline');
  }
}

function formatUptime(seconds) {
  const total = Math.round(seconds || 0);
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.round(total / 60)}m`;
  return `${(total / 3600).toFixed(1)}h`;
}

function renderSensors(data) {
  const cards = [
    ['CPU', `${data.cpu.percent}%`, `${data.cpu.cores} cores · load ${data.cpu.load_1m}`, data.cpu.percent, ''],
    ['MEMORY', `${data.memory.percent}%`, `${data.memory.used_gb} / ${data.memory.total_gb} GB`, data.memory.percent, 'lime'],
    ['DISK', `${data.disk.percent}%`, `${data.disk.free_gb} GB free`, data.disk.percent, 'amber'],
    ['NETWORK', `${data.network.recv_mb} MB`, `${data.network.sent_mb} MB sent`, 0, ''],
    ['PROCESS', `${data.process.rss_mb} MB`, `pid ${data.process.pid} · ${data.process.threads} threads`, data.process.cpu_percent, ''],
    ['HOST', esc(data.host), `${esc(data.platform)} · py ${esc(data.python)}`, 0, ''],
  ];
  const cores = (data.cpu.per_core || [])
    .map((value) => `<span class="bar" style="display:inline-block;width:14px;margin-right:2px"><i style="width:${Math.min(100, value)}%;height:10px"></i></span>`)
    .join('');
  $('#sensors').innerHTML =
    cards
      .map(
        ([label, value, sub, percent, tone]) => `<div class="sensor">
          <div class="s-label">${label}</div>
          <div class="s-value">${value}</div>
          <div class="s-sub">${sub}</div>
          ${percent ? `<div class="bar ${tone}"><i style="width:${Math.min(100, percent)}%"></i></div>` : ''}
        </div>`,
      )
      .join('') +
    `<div class="sensor"><div class="s-label">CORES</div><div class="s-sub" style="margin-top:8px">${cores}</div>
      <div class="s-sub">workspace<br><span class="dim">${esc(data.workspace)}</span></div></div>`;
}

function renderTelemetry(data) {
  const rows = [
    ['CPU', `${data.cpu.percent}%`, data.cpu.percent],
    ['MEM', `${data.memory.percent}%`, data.memory.percent],
    ['DISK', `${data.disk.percent}%`, data.disk.percent],
    ['PROC', `${data.process.cpu_percent}%`, Math.min(100, data.process.cpu_percent)],
  ];
  $('#diagBars').innerHTML = rows
    .map(
      ([label, value, percent]) => `<div class="metric">
        <div class="m-top"><span>${label}</span><b>${value}</b></div>
        <div class="bar"><i style="width:${Math.min(100, percent || 0)}%"></i></div>
      </div>`,
    )
    .join('');
}

function renderMatrix(health) {
  const rows = state.providers.map((provider) => {
    const status = health[provider.slug] || (provider.configured ? 'unknown' : 'unconfigured');
    return `<tr><td>${esc(provider.label)}</td><td class="dim">${esc(provider.model)}</td>
      <td class="${esc(status)}">${esc(status)}</td>
      <td class="dim">${provider.free ? 'free' : 'paid'}${provider.vision ? ' · vision' : ''}</td></tr>`;
  });
  $('#providerTable').innerHTML =
    '<thead><tr><th>provider</th><th>model</th><th>state</th><th>tier</th></tr></thead><tbody>' +
    (rows.join('') || '<tr><td colspan="4" class="dim">backend offline</td></tr>') +
    '</tbody>';
}

/* ------------------------------------------------------------------- link */

function bindLink() {
  $('#apiEndpoint').value = API;
  $('#saveApi').addEventListener('click', () => {
    const value = $('#apiEndpoint').value.trim().replace(/\/+$/, '');
    if (value) localStorage.setItem('kingsley.api', value);
    else localStorage.removeItem('kingsley.api');
    location.reload();
  });
  $('#googleConnectBtn').addEventListener('click', async () => {
    try {
      const result = await api('/api/google/connect');
      window.open(result.url, '_blank', 'noreferrer');
      toast('authorise Google in the new tab, then return here');
    } catch (error) {
      toast(error.message, true);
    }
  });
}

/* ----------------------------------------------------------------- stream */

function connectStream() {
  if (!('EventSource' in window)) return;
  if (state.stream) state.stream.close();
  const stream = new EventSource(`${API}/api/events/stream`);
  state.stream = stream;
  stream.onopen = () => {
    const dot = $('#streamDot');
    dot.className = 'dot live';
  };
  stream.onerror = () => {
    const dot = $('#streamDot');
    if (dot) dot.className = 'dot bad';
  };
  stream.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      prependEvent(data);
    } catch (_error) {
      /* ignore malformed frame */
    }
  };
}

function prependEvent(event) {
  const feed = $('#eventFeed');
  if (!feed) return;
  const node = document.createElement('div');
  node.className = `ev ${event.level || 'info'}`;
  node.innerHTML = `<span class="t">${clockTime(event.created_at)} · ${esc(event.source || 'kingsley')}</span><br>${esc(event.message)}`;
  feed.prepend(node);
  while (feed.children.length > 80) feed.lastChild.remove();
}

document.addEventListener('DOMContentLoaded', boot);
