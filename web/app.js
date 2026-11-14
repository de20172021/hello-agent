/* ============================================================
   HelloAgent 前端：会话管理 / 流式对话 / 工具调用 / 设置
   ============================================================ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const S = {
  config: null,
  models: [],
  conversations: [],
  tools: [],
  skills: [],
  mcp: [],
  conv: null,
  messages: [],
  activeId: null,
  live: null,
  // 运行状态：conversationId -> {id, status, source}（后端 /api/runs 的镜像）
  runs: {},
  runSeq: {},
  // conversationId -> true 表示已经点了停止、还在等这轮收尾
  stopping: {},
  queues: {},
  runsFingerprint: '',
  attach: null,
  // 待发送的附件（图片/文档），发送后清空
  pendingAttachments: [],
  pendingUploading: 0,
  // 会话产物：byMessage[message_id] = [file]，turn 是最后一轮的全部产物
  artifacts: { byMessage: {}, turn: [], removedByMessage: {}, turnRemoved: [] },
  liveArtifacts: [],
  liveRemovals: [],
  queue: [],
  queueNote: null,
  schedules: [],
  editingSchedule: null,
  theme: localStorage.getItem('lm-theme') || 'light',
  editingModelId: null,
  editingSkill: null,
  // 技能库被模型改过、还没刷新列表
  skillsDirty: false,
  search: '',
  ctx: { tokens: 0, limit: 32768, percent: 0, threshold: 0.75 },
  // 任务 = 直接跑在项目目录（space_id 为空）；空间 = 用户指定的工作目录
  taskScope: null,
  spaces: [],
  scope: readScope(),
  spaceMeta: { project_dir: '', default_parent: '' },
  expanded: new Set(),
  spaceMore: new Set(),
  batch: { on: false, ids: new Set() },
  view: 'chat',
  wsExpanded: new Set(),
  workspace: { open: false, entries: [], root: '' },
  dbg: { server: '', tools: [], tool: null },
};

/* ---------------------------- 任务与空间 ---------------------------- */

function readScope() {
  const raw = localStorage.getItem('lm-scope') || 'task';
  if (raw.startsWith('space:')) return { type: 'space', id: raw.slice(6) };
  return { type: 'task' };
}

function scopeKey(scope = S.scope) {
  return scope.type === 'space' ? `space:${scope.id}` : 'task';
}

function currentScope() {
  if (S.scope.type === 'space') {
    const hit = S.spaces.find((s) => s.id === S.scope.id);
    if (hit) return hit;
  }
  return S.taskScope || { type: 'task', id: null, name: '任务', icon: '⚡', path: '' };
}

function scopeOfConversation(conv) {
  if (conv?.space_id) return S.spaces.find((s) => s.id === conv.space_id) || currentScope();
  return S.taskScope || { type: 'task', id: null, name: '任务', icon: '⚡', path: '' };
}

/** 给文件类接口带上当前工作位置，后端据此决定读写哪个目录。 */
function withScope(url) {
  const sep = url.includes('?') ? '&' : '?';
  // 文件面板跟着当前对话的工作目录走；没有对话时按当前空间
  if (S.conv?.id) return `${url}${sep}conversation_id=${encodeURIComponent(S.conv.id)}`;
  const scope = currentScope();
  if (scope.type !== 'space' || !scope.id) return url;
  return `${url}${sep}space_id=${encodeURIComponent(scope.id)}`;
}

function shortPath(path) {
  if (!path) return '';
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) return path;
  return `…/${parts.slice(-2).join('/')}`;
}

async function loadSpaces() {
  const data = await api('/api/spaces');
  S.taskScope = data.task || null;
  S.spaces = data.spaces || [];
  S.spaceMeta = { project_dir: data.project_dir || '', default_parent: data.default_parent || '' };
  if (S.scope.type === 'space' && !S.spaces.some((s) => s.id === S.scope.id)) {
    S.scope = { type: 'task' };
    localStorage.setItem('lm-scope', 'task');
  }
  renderScopes();
  renderSpaceManage();
  updateScopeChip();
}

function renderScopes() {
  const box = $('#spaceGroups');
  if (!box) return;
  const spaces = (S.spaces || []).filter((s) => (s.kind || 'space') !== 'schedule');
  const countNode = $('#spaceCount');
  if (countNode) countNode.textContent = spaces.length ? ` (${spaces.length})` : '';
  box.innerHTML = '';
  if (!spaces.length) {
    box.innerHTML = '<div class="conv-empty">还没有空间<br/>点「＋」新建文件夹，或关联已有目录</div>';
    return;
  }
  spaces.forEach((space) => box.appendChild(spaceGroupRow(space)));
}

function spaceGroupRow(space) {
  const q = (S.search || '').toLowerCase();
  const convs = S.conversations.filter(
    (c) =>
      c.space_id === space.id &&
      (!q || (c.title || '').toLowerCase().includes(q) || (c.preview || '').toLowerCase().includes(q))
  );
  const active = S.scope.type === 'space' && S.scope.id === space.id;
  const expanded = S.expanded.has(space.id) || !!S.search;
  const wrap = el('div', `space-group${expanded ? ' open' : ''}${active ? ' active' : ''}`);
  const row = el('div', 'space-row');
  row.title = space.path || '';
  row.innerHTML = `
    <button class="sg-caret" title="展开 / 收起">${expanded ? '▾' : '▸'}</button>
    <span class="sg-ico">${esc(space.icon || '📁')}</span>
    <span class="sg-name">${esc(space.name)}</span>
    <span class="sg-count">${convs.length || space.conversation_count || 0}</span>
    <button class="sg-add" title="在这个空间新建对话">＋</button>
    <button class="sg-more" title="空间操作">⋯</button>`;
  row.onclick = (e) => {
    if (e.target.closest('.sg-add') || e.target.closest('.sg-more')) return;
    if (e.target.closest('.sg-caret')) {
      toggleSpace(space.id);
      return;
    }
    switchScope({ type: 'space', id: space.id });
    if (!S.expanded.has(space.id)) {
      S.expanded.add(space.id);
      saveExpanded();
      renderScopes();
    }
  };
  row.ondblclick = () => {
    switchScope({ type: 'space', id: space.id });
    createConversation();
  };
  row.querySelector('.sg-add').onclick = (e) => {
    e.stopPropagation();
    switchScope({ type: 'space', id: space.id });
    createConversation();
  };
  row.querySelector('.sg-more').onclick = (e) => {
    e.stopPropagation();
    openSpaceMenu(space, e.currentTarget);
  };
  wrap.appendChild(row);

  const body = el('div', 'space-convs');
  body.hidden = !expanded;
  const limit = S.spaceMore.has(space.id) ? convs.length : 6;
  if (!convs.length) body.appendChild(el('div', 'space-empty', '这个空间还没有对话'));
  convs.slice(0, limit).forEach((c) => body.appendChild(convRow(c, { compact: true })));
  if (convs.length > 6) {
    const more = el(
      'button',
      'space-more-link',
      S.spaceMore.has(space.id) ? '收起' : `查看更多 (${convs.length - 6})`
    );
    more.onclick = (e) => {
      e.stopPropagation();
      if (S.spaceMore.has(space.id)) S.spaceMore.delete(space.id);
      else S.spaceMore.add(space.id);
      renderScopes();
    };
    body.appendChild(more);
  }
  wrap.appendChild(body);
  return wrap;
}

function toggleSpace(id) {
  if (S.expanded.has(id)) S.expanded.delete(id);
  else S.expanded.add(id);
  saveExpanded();
  renderScopes();
}

function saveExpanded() {
  localStorage.setItem('lm-expanded', JSON.stringify([...S.expanded]));
}

function readExpanded() {
  try {
    return new Set(JSON.parse(localStorage.getItem('lm-expanded') || '[]'));
  } catch (_) {
    return new Set();
  }
}

async function switchScope(scope) {
  const next = scope.type === 'space' ? { type: 'space', id: scope.id } : { type: 'task' };
  const changed = scopeKey(next) !== scopeKey();
  if (S.view !== 'chat') closePage();
  S.scope = next;
  localStorage.setItem('lm-scope', scopeKey(next));
  if (changed) resetViewer();
  renderScopes();
  renderSidebar();
  updateScopeChip();
  updateHeader();
  if (S.workspace.open) loadWorkspace();
}

function leaveConversation() {
  detachStream();
  resetViewer();
  if (S.view !== 'chat') closePage();
  S.conv = null;
  S.activeId = null;
  S.messages = [];
  S.queue = [];
  S.live = null;
  S.artifacts = { byMessage: {}, turn: [], removedByMessage: {}, turnRemoved: [] };
  renderQueue();
  renderChat();
  updateHeader();
  syncRunUi();
}

function updateScopeChip() {
  const chip = $('#scopeChip');
  if (!chip) return;
  const scope = S.conv ? scopeOfConversation(S.conv) : currentScope();
  const isTask = !scope || scope.type === 'task' || !scope.id;
  chip.className = `scope-chip${isTask ? ' is-task' : ''}`;
  chip.innerHTML = `
    <span class="chip-ico">${esc(scope.icon || (isTask ? '⚡' : '📁'))}</span>
    <span class="chip-name">${esc(scope.name || '任务')}</span>
    <svg viewBox="0 0 24 24" width="12" height="12"><path d="m7 10 5 5 5-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  chip.title = `${isTask ? '任务' : '空间'} · ${scope.path || ''}（点击切换）`;
}

function closeFloating() {
  $$('.float-menu').forEach((node) => node.remove());
  document.removeEventListener('click', closeFloating, true);
}

function openFloating(anchor, build) {
  closeFloating();
  const menu = el('div', 'float-menu');
  build(menu);
  document.body.appendChild(menu);
  const rect = anchor.getBoundingClientRect();
  const width = menu.offsetWidth || 230;
  let left = rect.left;
  if (left + width > window.innerWidth - 12) left = window.innerWidth - width - 12;
  menu.style.left = `${Math.max(10, left)}px`;
  menu.style.top = `${rect.bottom + 6}px`;
  setTimeout(() => document.addEventListener('click', closeFloating, true), 0);
  return menu;
}

function openSpaceMenu(space, anchor) {
  openFloating(anchor, (menu) => {
    const head = el('div', 'float-head', `${esc(space.icon || '📁')} ${esc(space.name)}`);
    head.title = space.path || '';
    menu.appendChild(head);
    const actions = [
      ['在此空间新建对话', () => { switchScope({ type: 'space', id: space.id }); createConversation(); }],
      ['编辑空间', () => openSpaceModal(space)],
      ['在文件管理器中打开', () => revealPath(space.path)],
      ['复制目录路径', () => copyText(space.path)],
      ['—', null],
      ['移除空间（保留文件）', () => removeSpace(space)],
    ];
    actions.forEach(([label, fn]) => {
      if (!fn) {
        menu.appendChild(el('div', 'float-sep'));
        return;
      }
      const btn = el('button', 'float-item', esc(label));
      if (label.startsWith('移除')) btn.classList.add('danger');
      btn.onclick = () => {
        closeFloating();
        fn();
      };
      menu.appendChild(btn);
    });
  });
}

function openScopeMenu(anchor) {
  openFloating(anchor, (menu) => {
    menu.appendChild(el('div', 'float-head', '切换工作位置'));
    const items = [S.taskScope, ...S.spaces.filter((x) => (x.kind || 'space') !== 'schedule')].filter(Boolean);
    items.forEach((item) => {
      const isTask = item === S.taskScope;
      const active = isTask ? S.scope.type === 'task' : S.scope.type === 'space' && S.scope.id === item.id;
      const btn = el(
        'button',
        `float-item${active ? ' active' : ''}`,
        `<span class="fi-ico">${esc(item.icon || (isTask ? '⚡' : '📁'))}</span>
         <span class="fi-body"><span>${esc(item.name)}</span><small>${esc(shortPath(item.path))}</small></span>`
      );
      btn.onclick = () => {
        closeFloating();
        switchScope(isTask ? { type: 'task' } : { type: 'space', id: item.id });
      };
      menu.appendChild(btn);
    });
    menu.appendChild(el('div', 'float-sep'));
    const add = el('button', 'float-item', '＋ 新建空间');
    add.onclick = () => {
      closeFloating();
      openSpaceModal();
    };
    menu.appendChild(add);
    const manage = el('button', 'float-item', '管理空间…');
    manage.onclick = () => {
      closeFloating();
      openSettings('workspace');
    };
    menu.appendChild(manage);
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(String(text || ''));
    toast('路径已复制');
  } catch (_) {
    toast('复制失败');
  }
}

async function revealPath(path) {
  try {
    await api('/api/spaces/reveal', { method: 'POST', body: { path } });
    toast('已在文件管理器中打开');
  } catch (err) {
    toast(`打开失败：${err.message}`);
  }
}

async function removeSpace(space) {
  const count = space.conversation_count || 0;
  const tip = count
    ? `移除空间「${space.name}」？\n\n· 磁盘上的文件不会被删除\n· 里面的 ${count} 个对话会变成「任务」保留下来`
    : `移除空间「${space.name}」？\n\n磁盘上的文件不会被删除。`;
  if (!confirm(tip)) return;
  try {
    const data = await api(`/api/spaces/${space.id}`, { method: 'DELETE' });
    S.spaces = data.spaces?.spaces || [];
    if (S.scope.type === 'space' && S.scope.id === space.id) {
      await switchScope({ type: 'task' });
    }
    await loadSpaces();
    await loadConversations();
    toast('已移除空间');
  } catch (err) {
    toast(`移除失败：${err.message}`);
  }
}

/* ---------------------------- 空间弹窗 ---------------------------- */

const SPACE_ICONS = ['📁', '⚡', '🛠️', '✍️', '📊', '🧪', '🎨', '📚', '🧩', '🚀', '💡', '🗂️'];
const spaceState = { mode: 'created', icon: '📁', editing: null, browseFor: 'parent', dirPath: '', dirParent: null };

function renderSpaceIcons() {
  const box = $('#spaceIcons');
  box.innerHTML = '';
  SPACE_ICONS.forEach((icon) => {
    const btn = el('button', `emoji-btn${spaceState.icon === icon ? ' active' : ''}`, icon);
    btn.type = 'button';
    btn.onclick = () => {
      spaceState.icon = icon;
      renderSpaceIcons();
    };
    box.appendChild(btn);
  });
}

function spaceFolderName(name) {
  const cleaned = String(name || '')
    .trim()
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '-')
    .replace(/\s+/g, '-')
    .replace(/^[.\- ]+|[.\- ]+$/g, '');
  return (cleaned || 'space').slice(0, 48);
}

function renderSpacePreview() {
  const editing = spaceState.editing;
  const parent = $('#spaceParent').value.trim() || S.spaceMeta.default_parent || '';
  const name = $('#spaceName').value.trim() || '未命名空间';
  if (editing) {
    $('#spacePreview').textContent = `目录：${editing.path}（编辑不会移动文件）`;
    return;
  }
  if (spaceState.mode === 'linked') {
    const path = $('#spacePath').value.trim();
    $('#spacePreview').textContent = path ? `将关联：${path}` : '选一个已存在的文件夹，文件不会被移动';
    return;
  }
  const sep = parent.includes('\\') && !parent.includes('/') ? '\\' : '/';
  const base = parent.replace(/[\\/]+$/, '');
  $('#spacePreview').textContent = `将在 ${base}${sep}${spaceFolderName(name)} 创建新文件夹`;
}

function openSpaceModal(space = null) {
  spaceState.editing = space || null;
  spaceState.mode = space?.mode === 'linked' ? 'linked' : 'created';
  spaceState.icon = space?.icon || (spaceState.mode === 'linked' ? '🔗' : '📁');
  $('#spaceModalTitle').textContent = space ? '编辑空间' : '新建空间';
  $('#spaceModalSub').textContent = space
    ? '改名字、图标和说明；目录不会变'
    : '空间 = 一个有名字的工作目录，里面的对话共用同一批文件';
  $('#spaceName').value = space?.name || '';
  $('#spaceParent').value = space?.path ? space.path.split(/[\\/]/).slice(0, -1).join('/') || S.spaceMeta.default_parent : S.spaceMeta.default_parent || '';
  $('#spacePath').value = space?.path || '';
  $('#spaceInstructions').value = space?.instructions || '';
  $('#spaceMode').hidden = !!space;
  $('#spaceCreatePane').hidden = space ? true : spaceState.mode !== 'created';
  $('#spaceLinkPane').hidden = space ? true : spaceState.mode !== 'linked';
  $$('#spaceMode button').forEach((b) => b.classList.toggle('active', b.dataset.mode === spaceState.mode));
  $('#spaceSubmit').textContent = space ? '保存' : '创建并进入';
  $('#spaceError').hidden = true;
  renderSpaceIcons();
  renderSpacePreview();
  openModal('spaceModal');
  setTimeout(() => $('#spaceName').focus(), 60);
}

function spaceFormError(message) {
  const box = $('#spaceError');
  box.textContent = message;
  box.hidden = !message;
}

async function submitSpace() {
  const editing = spaceState.editing;
  const name = $('#spaceName').value.trim();
  if (!name) {
    spaceFormError('先给空间起个名字');
    $('#spaceName').focus();
    return;
  }
  const body = {
    name,
    icon: spaceState.icon,
    instructions: $('#spaceInstructions').value,
  };
  try {
    $('#spaceSubmit').disabled = true;
    if (editing) {
      const data = await api(`/api/spaces/${editing.id}`, { method: 'PATCH', body });
      S.spaces = data.spaces?.spaces || S.spaces;
      closeModal('spaceModal');
      await loadSpaces();
      await loadConversations();
      toast('空间已更新');
      return;
    }
    if (spaceState.mode === 'linked') {
      body.mode = 'linked';
      body.path = $('#spacePath').value.trim();
      if (!body.path) {
        spaceFormError('请选择要关联的文件夹');
        return;
      }
    } else {
      body.mode = 'created';
      body.parent = $('#spaceParent').value.trim() || S.spaceMeta.default_parent;
    }
    const data = await api('/api/spaces', { method: 'POST', body });
    const created = data.space;
    closeModal('spaceModal');
    await loadSpaces();
    S.scope = { type: 'space', id: created.id };
    localStorage.setItem('lm-scope', scopeKey());
    if (convSpace && !S.expanded.has(convSpace)) {
      S.expanded.add(convSpace);
      saveExpanded();
    }
    renderScopes();
    leaveConversation();
    if (S.workspace.open) loadWorkspace();
    await loadConversations();
    updateScopeChip();
    toast(`已进入「${created.name}」`);
  } catch (err) {
    spaceFormError(err.message);
  } finally {
    $('#spaceSubmit').disabled = false;
  }
}

async function openDirBrowser(target, startPath) {
  spaceState.browseFor = target;
  await loadDir(startPath || '');
  openModal('dirModal');
}

async function loadDir(path) {
  try {
    const data = await api('/api/spaces/browse', { method: 'POST', body: { path } });
    spaceState.dirPath = data.path || '';
    spaceState.dirParent = data.parent || null;
    const places = $('#dirPlaces');
    places.innerHTML = '';
    (data.places || []).forEach((place) => {
      const btn = el('button', 'dir-place', esc(place.label));
      btn.title = place.path;
      btn.onclick = () => loadDir(place.path);
      places.appendChild(btn);
    });
    if (data.drives?.length) {
      data.drives.forEach((drive) => {
        const btn = el('button', 'dir-place drive', esc(drive));
        btn.onclick = () => loadDir(drive);
        places.appendChild(btn);
      });
    }
    const crumb = $('#dirPath');
    crumb.innerHTML = '';
    if (!spaceState.dirPath) {
      crumb.textContent = '选择一个位置开始';
    } else {
      const pathEl = el('span', 'dir-crumb-path', esc(spaceState.dirPath));
      pathEl.title = spaceState.dirPath;
      crumb.appendChild(pathEl);
    }
    const list = $('#dirList');
    list.innerHTML = '';
    if (!data.path) {
      list.innerHTML = '<div class="dir-empty">从上方选一个位置，或直接用输入框填路径</div>';
      return;
    }
    if (!(data.dirs || []).length) {
      list.innerHTML = '<div class="dir-empty">这个目录下没有子文件夹<br/>可以直接选它</div>';
      return;
    }
    (data.dirs || []).forEach((dir) => {
      const row = el('div', 'dir-row');
      row.innerHTML = `<span class="ico">📁</span><span class="name">${esc(dir.name)}</span><span class="go">进入 ›</span>`;
      row.title = dir.path;
      row.onclick = () => loadDir(dir.path);
      list.appendChild(row);
    });
  } catch (err) {
    toast(`读取目录失败：${err.message}`, 2600);
  }
}

function pickDir() {
  if (!spaceState.dirPath) {
    toast('先进入一个文件夹');
    return;
  }
  if (spaceState.browseFor === 'link') {
    $('#spacePath').value = spaceState.dirPath;
    const name = spaceState.dirPath.split(/[\\/]/).filter(Boolean).pop();
    if (!$('#spaceName').value.trim() && name) $('#spaceName').value = name;
  } else {
    $('#spaceParent').value = spaceState.dirPath;
  }
  renderSpacePreview();
  closeModal('dirModal');
}

/* ---------------------------- 基础工具 ---------------------------- */

function el(tag, cls, html) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html != null) node.innerHTML = html;
  return node;
}

function esc(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

let toastTimer = null;
function toast(message, ms = 1900) {
  const box = $('#toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), ms);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function fmtWhen(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n) => String(n).padStart(2, '0');
  if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}/${d.getDate()}`;
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
}

function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return '今天';
  if (d.toDateString() === yesterday.toDateString()) return '昨天';
  if (Date.now() - ts * 1000 < 7 * 86400000) return '最近 7 天';
  return '更早';
}

/* ---------------------------- Markdown ---------------------------- */

if (window.marked) {
  marked.setOptions({ gfm: true, breaks: true });
}

function sanitize(html) {
  const wrap = document.createElement('div');
  wrap.innerHTML = html;
  wrap.querySelectorAll('script,style,iframe,object,embed,link,meta,form,input').forEach((n) => n.remove());
  wrap.querySelectorAll('*').forEach((node) => {
    [...node.attributes].forEach((attr) => {
      const name = attr.name.toLowerCase();
      if (name.startsWith('on')) node.removeAttribute(attr.name);
      if ((name === 'href' || name === 'src') && /^\s*javascript:/i.test(attr.value)) {
        node.removeAttribute(attr.name);
      }
    });
  });
  return wrap.innerHTML;
}

function mdToHtml(text) {
  if (!text) return '';
  if (!window.marked) return `<p>${esc(text)}</p>`;
  try {
    return sanitize(marked.parse(text));
  } catch (_) {
    return `<p>${esc(text)}</p>`;
  }
}

const LANG_LABEL = { js: 'JavaScript', ts: 'TypeScript', py: 'Python', bash: 'Bash', sh: 'Shell', json: 'JSON', html: 'HTML', css: 'CSS', sql: 'SQL' };

function enhance(container) {
  container.querySelectorAll('pre > code').forEach((code) => {
    const pre = code.parentElement;
    if (pre.parentElement?.classList.contains('code-block')) return;
    const langClass = [...code.classList].find((c) => c.startsWith('language-'));
    const lang = langClass ? langClass.replace('language-', '') : '';
    if (window.hljs) {
      try {
        hljs.highlightElement(code);
      } catch (_) {}
    }
    const block = el('div', 'code-block');
    const head = el('div', 'code-head');
    head.innerHTML = `<span>${esc(LANG_LABEL[lang] || lang || 'code')}</span>`;
    const copy = el('button', 'code-copy', '复制');
    copy.onclick = async () => {
      await navigator.clipboard.writeText(code.innerText);
      copy.textContent = '已复制';
      setTimeout(() => (copy.textContent = '复制'), 1400);
    };
    head.appendChild(copy);
    pre.replaceWith(block);
    block.append(head, pre);
  });
  // 外部链接新窗口打开
  container.querySelectorAll('a[href^="http"]').forEach((a) => {
    a.target = '_blank';
    a.rel = 'noreferrer noopener';
  });
}

/* ---------------------------- 主题 ---------------------------- */

function applyTheme(theme) {
  S.theme = theme;
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('lm-theme', theme);
  $('#hljsLight').disabled = theme === 'dark';
  $('#hljsDark').disabled = theme !== 'dark';
}

/* ---------------------------- 侧边栏 ---------------------------- */

function renderSidebar() {
  const list = $('#taskList');
  if (!list) return;
  const q = (S.search || '').toLowerCase();
  const hit = (c) =>
    !q ||
    (c.title || '').toLowerCase().includes(q) ||
    (c.preview || '').toLowerCase().includes(q);
  const tasks = S.conversations.filter((c) => !c.space_id && hit(c));
  const count = $('#taskCount');
  if (count) count.textContent = tasks.length ? ` (${tasks.length})` : '';
  list.innerHTML = '';
  if (!tasks.length) {
    list.innerHTML = `<div class="conv-empty">${
      S.search ? '没有匹配的对话' : '还没有对话<br/>点上方「新建任务」开始'
    }</div>`;
  } else {
    let lastGroup = '';
    tasks.forEach((conv) => {
      const group = dayLabel(conv.updated_at);
      if (group !== lastGroup) {
        list.appendChild(el('div', 'conv-group-label', group));
        lastGroup = group;
      }
      list.appendChild(convRow(conv, { compact: true }));
    });
  }
  renderScopes();
  updateBatchBar();
  const badge = $('#navSchedBadge');
  if (badge) {
    badge.textContent = S.schedules.length;
    badge.hidden = !S.schedules.length;
  }
}

function convRow(conv, opts = {}) {
  const run = S.runs[conv.id];
  const busy = run && (run.status === 'running' || run.status === 'queued');
  const queued = S.queues[conv.id]?.count || 0;
  const selected = S.batch.ids.has(conv.id);
  const row = el(
    'div',
    `conv-item${opts.compact ? ' compact' : ''}${conv.id === S.activeId ? ' active' : ''}${
      busy ? ' busy' : ''
    }${selected ? ' selected' : ''}`
  );
  row.dataset.id = conv.id;
  row.innerHTML = `
    ${S.batch.on ? `<span class="conv-check${selected ? ' on' : ''}"></span>` : ''}
    <div class="conv-title">${busy ? '<span class="run-dot"></span>' : ''}${esc(conv.title || '新对话')}</div>
    ${
      opts.compact
        ? `<span class="conv-time">${fmtShortTime(conv.updated_at)}</span>`
        : `<div class="conv-meta">${esc(conv.preview || '还没有消息')}</div>`
    }
    ${queued ? `<span class="conv-queue-badge" title="队列里有 ${queued} 条待发送">${queued}</span>` : ''}
    ${S.batch.on ? '' : '<button class="conv-del" title="删除对话">✕</button>'}`;
  row.onclick = (e) => {
    if (e.target.closest('.conv-del')) return;
    if (S.batch.on) {
      toggleBatchSel(conv.id);
      return;
    }
    openConversation(conv.id);
  };
  const del = row.querySelector('.conv-del');
  if (del) {
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`删除对话「${conv.title}」？此操作不可恢复。`)) return;
      if (S.activeId === conv.id) detachStream();
      await api(`/api/conversations/${conv.id}`, { method: 'DELETE' });
      if (S.activeId === conv.id) {
        S.activeId = null;
        S.conv = null;
        S.messages = [];
        S.queue = [];
        renderQueue();
        renderChat();
        updateHeader();
        syncRunUi();
      }
      await loadConversations();
      toast('已删除');
    };
  }
  return row;
}

function fmtShortTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  if (d.toDateString() === now.toDateString()) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (now - d < 6 * 86400000) return `${d.getMonth() + 1}-${pad(d.getDate())}`;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/* ---------------------------- 批量选择 ---------------------------- */

function setBatchMode(on) {
  S.batch.on = on;
  if (!on) S.batch.ids.clear();
  const btn = $('#batchBtn');
  if (btn) {
    btn.textContent = on ? '完成' : '选择';
    btn.classList.toggle('on', on);
  }
  renderSidebar();
}

function toggleBatchSel(id) {
  if (S.batch.ids.has(id)) S.batch.ids.delete(id);
  else S.batch.ids.add(id);
  renderSidebar();
}

function updateBatchBar() {
  const bar = $('#batchBar');
  if (!bar) return;
  bar.hidden = !S.batch.on;
  const info = $('#batchInfo');
  if (info) info.textContent = `已选 ${S.batch.ids.size} 项`;
  const del = $('#batchDelete');
  if (del) del.disabled = !S.batch.ids.size;
}

async function deleteBatchConversations() {
  const ids = [...S.batch.ids];
  if (!ids.length) return toast('先选几条对话');
  if (!confirm(`删除选中的 ${ids.length} 条对话？对话里的消息会一起删掉，不可恢复。`)) return;
  for (const id of ids) {
    if (S.activeId === id) detachStream();
    try {
      await api(`/api/conversations/${id}`, { method: 'DELETE' });
    } catch (_) {}
  }
  if (ids.includes(S.activeId)) {
    S.activeId = null;
    S.conv = null;
    S.messages = [];
    renderQueue();
    renderChat();
    updateHeader();
    syncRunUi();
  }
  setBatchMode(false);
  await loadConversations();
  toast(`已删除 ${ids.length} 条对话`);
}

/* ---------------------------- 会话 ---------------------------- */

async function loadConversations() {
  const data = await api('/api/conversations');
  S.conversations = data.conversations || [];
  renderSidebar();
}

// 当前对话是不是「一句话都还没发」：没消息、没在生成、也没有排队
function conversationIsBlank(conv = S.conv) {
  if (!conv) return false;
  if (runOf(conv.id)) return false;
  if (S.messages.length) return false;
  return true;
}

// 连点「新建对话」时只发一次请求：后面的点击复用同一个在建任务
let convCreating = null;

async function createConversation() {
  if (convCreating) return convCreating;
  convCreating = createConversationNow();
  try {
    return await convCreating;
  } finally {
    convCreating = null;
  }
}

async function createConversationNow() {
  const state = thinkState();
  const scope = currentScope();
  // 当前对话还没发出任何消息 → 不新建，维持现有界面（避免连点堆出一堆空对话）
  if (conversationIsBlank()) {
    const convSpace = S.conv.space_id || null;
    const sameScope = scope.type === 'space' ? convSpace === scope.id : !convSpace;
    if (sameScope) {
      if (S.view !== 'chat') closePage();
      toast('当前对话还没发出消息，接着用这个就行');
      return S.conv;
    }
  }
  const conv = await api('/api/conversations', {
    method: 'POST',
    body: {
      model_id: $('#modelSelect').value || null,
      thinking: !!state.thinking,
      thinking_level: state.level,
      skills: activeSkillNames(),
      space_id: scope.type === 'space' ? scope.id : null,
    },
  });
  S.conversations.unshift(conv);
  await openConversation(conv.id);
  return conv;
}

/* ---------------------------- 运行状态与重连 ---------------------------- */

function runOf(convId) {
  const run = convId ? S.runs[convId] : null;
  if (!run) return null;
  return run.status === 'done' || run.status === 'stopped' || run.status === 'error' ? null : run;
}

function busyHere() {
  return !!runOf(S.activeId);
}

function detachStream() {
  if (S.attach?.ctrl) {
    try {
      S.attach.ctrl.abort();
    } catch (_) {}
  }
  S.attach = null;
}

let syncing = false;
async function syncRuns() {
  if (syncing) return;
  syncing = true;
  const before = { ...S.runs };
  try {
    const data = await api('/api/runs');
    const next = {};
    (data.runs || []).forEach((run) => {
      next[run.conversation_id] = run;
    });
    const prevRun = S.activeId ? before[S.activeId] : null;
    const justFinished =
      !!prevRun &&
      (prevRun.status === 'running' || prevRun.status === 'queued') &&
      !runOf(S.activeId) &&
      !S.attach;

    const fingerprint = JSON.stringify([next, data.queues || {}]);
    const changed = fingerprint !== S.runsFingerprint;
    S.runsFingerprint = fingerprint;
    S.runs = next;
    S.queues = data.queues || {};
    // 已经结束的会话不再保留「正在停止」标记
    Object.keys(S.stopping).forEach((id) => {
      if (!runOf(id)) delete S.stopping[id];
    });

    if (changed) {
      renderSidebar();
      syncRunUi();
      await loadQueue();
    }
    if (justFinished) await refreshAfterTurn();
  } catch (_) {
    // 后端没起来时静默，下一轮再试
  } finally {
    syncing = false;
  }
}

function syncRunUi() {
  const run = runOf(S.activeId);
  const busy = !!run;
  const stopping = !!run && (run.status === 'stopping' || S.stopping[S.activeId]);
  $('#stopBtn').hidden = !busy;
  $('#stopBtn').disabled = stopping;
  $('#stopBtn').textContent = stopping ? '正在停止…' : '停止';
  $('#sendBtn').classList.toggle('queue-mode', busy);
  $('#sendBtn').title = busy ? '加入队列（当前正在生成）' : '发送';
  $('#footHint').textContent = busy
    ? stopping
      ? '正在停止这轮生成…'
      : run.status === 'queued'
        ? '⏳ 模型忙，已排队：空下来就自动开始'
        : '正在生成 · 现在发送会进队列，可以排序 / 编辑 / 插队'
    : 'Enter 发送 · Shift + Enter 换行 · 输入 / 调起技能 · Ctrl+K 新对话';
  $('#input').placeholder = busy ? '继续输入，Enter 加入队列…' : '给 HelloAgent 发消息…';
}

async function attachRun(convId, runId) {
  detachStream();
  const ctrl = new AbortController();
  S.attach = { runId, convId, ctrl };
  S.runSeq[runId] = 0;
  try {
    // from_seq=0 + fresh=1：把这一轮已经产生的事件整体重放一遍，界面就是完整的过程
    const res = await fetch(`/api/runs/${runId}/stream?from_seq=0&fresh=1`, {
      signal: ctrl.signal,
    });
    if (!res.ok) return;
    await readSSE(res, (ev) => {
      if (S.activeId !== convId) return;
      if (ev.seq) S.runSeq[runId] = ev.seq;
      handleEvent(ev);
    });
  } catch (err) {
    if (err.name !== 'AbortError') console.warn('重连运行失败', err);
  } finally {
    if (S.attach?.ctrl === ctrl) S.attach = null;
    syncRunUi();
    if (S.activeId === convId && !runOf(convId)) await refreshAfterTurn();
  }
}

async function openConversation(id) {
  if (S.activeId === id && S.conv) {
    // 同一会话：只补一下状态（例如点了两次）
    syncRunUi();
    return;
  }
  // 从定时任务 / 用量整页回到对话
  if (S.view !== 'chat') closePage();
  // 切会话不再打断生成：只是不再观察当前这条流，后端继续跑
  detachStream();
  resetViewer();
  S.live = null;
  S.queueNote = null;
  const data = await api(`/api/conversations/${id}/messages`);
  S.conv = data.conversation;
  S.messages = data.messages || [];
  S.activeId = id;
  // 打开对话时跟随它所属的工作位置（任务 / 空间）
  const convSpace = S.conv?.space_id || null;
  const scopeMatches = S.scope.type === 'space' ? convSpace === S.scope.id : !convSpace;
  if (!scopeMatches) {
    S.scope = convSpace ? { type: 'space', id: convSpace } : { type: 'task' };
    localStorage.setItem('lm-scope', scopeKey());
    renderScopes();
  }
  renderSidebar();
  await renderChat();
  updateHeader();
  updateScopeChip();
  refreshContext();
  await loadQueue();
  syncRunUi();
  if (S.workspace.open) loadWorkspace();
  if (window.innerWidth <= 860) $('#app').classList.add('collapsed');
  const run = runOf(id);
  if (run) attachRun(id, run.id);
}

async function refreshContext() {
  if (!S.activeId) return;
  try {
    const data = await api(`/api/conversations/${S.activeId}/context`);
    if (data.usage) {
      S.ctx = data.usage;
      updateCtxMeter();
    }
    S.contextInfo = data;
  } catch (_) {}
}

const THINK_LABEL = { off: '关', low: '轻度', medium: '标准', high: '深入' };
const THINK_BUDGET = { low: 320, medium: 900, high: 2400 };

function updateHeader() {
  const conv = S.conv;
  $('#convTitle').textContent = conv?.title || '新对话';
  const model = S.models.find((m) => m.id === conv?.model_id);
  const count = S.messages.filter((m) => m.role === 'user').length;
  $('#convSub').textContent = conv
    ? `${model?.label || conv.model_id || '未选择模型'} · ${count} 轮对话`
    : '直接输入，模型可以读写文件、执行命令';
  $('#modelSelect').value = conv?.model_id || S.config?.active_model_id || '';
  // 参数提示
  const t = conv?.temperature;
  const p = conv?.top_p;
  $('#paramHint').textContent =
    t != null ? `temp ${Number(t).toFixed(2)} · top_p ${Number(p ?? 0.95).toFixed(2)} · ${conv.max_tokens || 2048} tokens` : '';
  updateModelChip();

  // 思考开关
  const thinking = conv ? !!conv.thinking : !!(S.pendingThink?.thinking);
  const level = conv?.thinking_level || S.pendingThink?.level || S.config?.defaults?.thinking_level || 'medium';
  $('#thinkChip').classList.toggle('on', thinking);
  $('#thinkChip').classList.toggle('off', !thinking);
  $('#thinkLabel').textContent = thinking ? THINK_LABEL[level] || '标准' : '关';
  $('#thinkToggle').checked = thinking;
  $$('#thinkLevels button').forEach((b) => {
    b.classList.toggle('active', b.dataset.level === level);
    b.disabled = !thinking;
  });
  $('#thinkHint').textContent = thinking
    ? `思考预算约 ${THINK_BUDGET[level] || 900} tokens，用尽后自动进入回答`
    : '关闭时模型不输出思考过程，直接给答案';

  renderActiveSkills();
  updateCtxMeter();
  updateScopeChip();
}

function renderActiveSkills() {
  const wrap = $('#activeSkills');
  const names = activeSkillNames();
  wrap.innerHTML = '';
  wrap.hidden = !names.length;
  names.forEach((name) => {
    const skill = S.skills.find((s) => s.slug === name);
    const pill = el('span', 'skill-pill');
    pill.innerHTML = `<span>${esc(skill?.name || name)}</span><button title="移除">✕</button>`;
    pill.querySelector('button').onclick = () => toggleSkill(name, false);
    wrap.appendChild(pill);
  });
  $('#skillLabel').textContent = String(names.length);
  $('#skillChip').classList.toggle('on', names.length > 0);
}

function updateCtxMeter() {
  const usage = S.ctx || {};
  const percent = Math.min(100, Number(usage.percent || 0));
  $('#ctxFill').style.width = `${Math.max(2, percent)}%`;
  $('#ctxText').textContent = `${percent >= 10 ? Math.round(percent) : percent.toFixed(1)}%`;
  const meter = $('#ctxMeter');
  const limit = Number(usage.limit || 0);
  const trigger = Number(usage.trigger || 0);
  // 「按占比」看 threshold，「按 token 数」看触发 token 折算成窗口占比（超过 100% 就当 100%）
  const warnAt = usage.mode === 'ratio'
    ? Number(usage.threshold || 0.75) * 100
    : limit && trigger ? Math.min(100, (trigger / limit) * 100) : 100;
  meter.classList.toggle('warn', percent >= warnAt);
  meter.classList.toggle('danger', percent >= 90);
  const how = usage.mode === 'ratio'
    ? `窗口的 ${Math.round(Number(usage.threshold || 0.75) * 100)}%`
    : `${trigger || 0} tokens`;
  meter.title = `上下文占用 ${usage.tokens || 0} / ${limit || 0} tokens（约 ${percent}%）\n达到 ${how} 时自动压缩\n点击查看或手动压缩`;
}

/* ---------------------------- 消息渲染 ---------------------------- */

// 会产出文件的工具（后端 /artifacts 用的是同一份口径）
const PRODUCE_TOOLS = ['write_file', 'edit_file'];

const KIND_META = {
  image: { icon: '🖼', label: '图片' },
  markdown: { icon: '📝', label: 'Markdown' },
  html: { icon: '🌐', label: '网页' },
  table: { icon: '📊', label: '表格' },
  code: { icon: '🧩', label: '代码' },
  text: { icon: '📄', label: '文本' },
  document: { icon: '📕', label: '文档' },
  other: { icon: '📦', label: '文件' },
};

function kindMeta(file) {
  return KIND_META[file?.kind] || KIND_META.other;
}

function guessKind(name) {
  const lower = String(name || '').toLowerCase();
  if (/\.(png|jpe?g|gif|webp|svg|bmp)$/.test(lower)) return 'image';
  if (/\.(md|markdown|mdx)$/.test(lower)) return 'markdown';
  if (/\.(html?)$/.test(lower)) return 'html';
  if (/\.(csv|tsv)$/.test(lower)) return 'table';
  if (/\.(txt|log)$/.test(lower)) return 'text';
  if (/\.(pdf|docx?|pptx?|xlsx?|zip)$/.test(lower)) return 'document';
  if (/\.[a-z0-9]+$/.test(lower)) return 'code';
  return 'other';
}

function rawUrl(file) {
  return withScope(file.url || `/api/workspace/raw?path=${encodeURIComponent(file.path)}`);
}

async function loadArtifacts(convId) {
  const empty = { byMessage: {}, turn: [], removedByMessage: {}, turnRemoved: [] };
  if (!convId) {
    S.artifacts = empty;
    return empty;
  }
  try {
    const data = await api(`/api/conversations/${convId}/artifacts`);
    const byMessage = {};
    const removedByMessage = {};
    (data.artifacts || []).forEach((file) => {
      const key = file.message_id || '_';
      if (!byMessage[key]) byMessage[key] = [];
      byMessage[key].push(file);
    });
    (data.removed || []).forEach((item) => {
      const key = item.message_id || '_';
      if (!removedByMessage[key]) removedByMessage[key] = [];
      removedByMessage[key].push(item.path);
    });
    S.artifacts = {
      byMessage,
      turn: data.turn || [],
      removedByMessage,
      // turn_removed 的条目是 {path, message_id} 对象，统一取 path 字符串
      turnRemoved: (data.turn_removed || [])
        .map((item) => (typeof item === 'string' ? item : item && item.path))
        .filter(Boolean),
    };
  } catch (_) {
    S.artifacts = empty;
  }
  return S.artifacts;
}

async function renderChat() {
  const box = $('#messages');
  box.innerHTML = '';
  const has = S.messages.length > 0;
  $('#welcome').hidden = has;
  const scope = currentScope();
  if (!has) {
    $('#welcomeTitle').textContent =
      scope.type === 'space' ? `在「${scope.name}」里开始` : '开始一个任务';
    $('#welcomeSub').textContent = scope.path
      ? `${scope.path} · 模型跑在本机，文件都留在这个目录里`
      : '模型完全跑在你的本机上，对话与文件都只留在本地。';
    // 刚升级上来的用户：历史对话都在「默认空间」里，别让人以为丢了
    const hint = $('#welcomeHint');
    const inSpaces = S.spaces.reduce((sum, sp) => sum + (sp.conversation_count || 0), 0);
    if (scope.type === 'task' && !(S.taskScope?.conversation_count || 0) && inSpaces) {
      const first = S.spaces.find((sp) => sp.conversation_count);
      hint.innerHTML = `历史对话都在左侧的空间里，点一下「${esc(first?.name || '空间')}」就能回去。`;
      hint.hidden = false;
    } else {
      hint.hidden = true;
    }
  }
  if (!has) return;

  const toolResults = {};
  S.messages.forEach((m) => {
    if (m.role === 'tool') toolResults[m.tool_call_id || m.id] = m;
  });

  // 产物清单由后端按「哪条消息产的」算好，前端只负责画卡片
  const artifacts = await loadArtifacts(S.activeId);

  S.messages.forEach((msg) => {
    if (msg.role === 'user') box.appendChild(userNode(msg));
    else if (msg.role === 'assistant') {
      box.appendChild(
        assistantNode(
          msg,
          toolResults,
          artifacts.byMessage[msg.id] || [],
          artifacts.removedByMessage[msg.id] || []
        )
      );
    }
  });

  // 最后一条回答之后：本轮产物汇总卡片
  if (artifacts.turn.length || artifacts.turnRemoved.length) {
    box.appendChild(turnSummaryNode(artifacts.turn, artifacts.turnRemoved));
  }
  scrollToBottom(true);
}

function userNode(msg) {
  const node = el('div', 'msg user');
  node.dataset.id = msg.id;
  const bubble = el('div', 'bubble');
  if (msg.content) bubble.appendChild(el('div', 'bubble-text', esc(msg.content)));
  const atts = msg.attachments || [];
  if (atts.length) bubble.appendChild(userAttachmentsNode(atts));
  node.appendChild(bubble);
  return node;
}

function userAttachmentsNode(files) {
  const wrap = el('div', 'msg-files');
  files.forEach((file) => {
    const meta = kindMeta(file);
    const chip = el('button', 'msg-file');
    if (file.kind === 'image') {
      chip.classList.add('is-image');
      chip.innerHTML = `<img src="${rawUrl(file)}" alt="${esc(file.name)}" loading="lazy" />`;
    } else {
      chip.innerHTML = `<span class="msg-file-ico">${meta.icon}</span>`;
    }
    chip.title = `${file.name} · ${meta.label} · ${fmtSize(file.size)}`;
    chip.onclick = () => openPreview(file);
    wrap.appendChild(chip);
  });
  return wrap;
}

function assistantNode(msg, toolResults = {}, files = [], removed = []) {
  const node = el('div', 'msg assistant');
  node.dataset.id = msg.id;
  const body = el('div', 'body');
  node.innerHTML = '<div class="avatar">LM</div>';
  node.appendChild(body);

  if (msg.reasoning) {
    const think = el('details', 'think');
    think.innerHTML = `<summary>思考过程</summary><div class="think-body">${esc(msg.reasoning)}</div>`;
    body.appendChild(think);
  }

  const stack = el('div', 'tool-stack');
  (msg.tool_calls || []).forEach((call) => {
    const fn = call.function || {};
    const result = toolResults[call.id];
    stack.appendChild(buildToolCard(fn.name, fn.arguments, result));
  });
  if (stack.children.length) body.appendChild(stack);

  const md = el('div', 'md');
  md.innerHTML = mdToHtml(msg.content);
  enhance(md);
  body.appendChild(md);

  if (files.length || removed.length) body.appendChild(artifactsNode(files, removed));

  if (msg.stats) body.appendChild(statsNode(msg.stats));
  else if (msg.role === 'assistant' && !msg.content) {
    body.appendChild(el('div', 'md', '<span class="caret"></span>'));
  }
  body.appendChild(actionsNode(msg));
  return node;
}

function fmtSize(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/* ---------------- 产物卡片 ---------------- */

function artifactCard(file) {
  const meta = kindMeta(file);
  const card = el('button', 'artifact-card');
  card.title = `${file.path} · ${meta.label} · ${fmtSize(file.size)} · 点击预览`;
  const thumb =
    file.kind === 'image'
      ? `<img src="${rawUrl(file)}" alt="" loading="lazy" />`
      : `<span class="artifact-ico">${meta.icon}</span>`;
  card.innerHTML = `
    <span class="artifact-thumb">${thumb}</span>
    <span class="artifact-info">
      <b>${esc(file.name || file.path)}</b>
      <span>${meta.label} · ${fmtSize(file.size)}</span>
    </span>`;
  card.onclick = () => openPreview(file);
  return card;
}

function artifactGrid(files) {
  const grid = el('div', 'artifact-grid');
  files.forEach((file) => grid.appendChild(artifactCard(file)));
  return grid;
}

function removedNode(paths) {
  const list = el('div', 'artifact-removed');
  list.innerHTML = paths
    .map((p) => (typeof p === 'string' ? p : p && p.path)) // 防御：条目可能是 {path} 对象
    .filter(Boolean)
    .map((p) => `<span class="removed-chip" title="${esc(p)}">🗑 ${esc(shortPath(p))}（已删除）</span>`)
    .join('');
  return list;
}

function artifactsNode(files, removed = []) {
  const wrap = el('div', 'artifact-block');
  const label = removed.length ? `产物 <b>${files.length}</b> · 删除 ${removed.length}` : `产物 <b>${files.length}</b>`;
  wrap.innerHTML = `<div class="artifact-block-head">${label}</div>`;
  if (files.length) wrap.appendChild(artifactGrid(files));
  if (removed.length) wrap.appendChild(removedNode(removed));
  return wrap;
}

function turnSummaryNode(files, removed = []) {
  const wrap = el('div', 'artifact-summary');
  const kinds = [...new Set(files.map((f) => kindMeta(f).label))];
  const head = [
    files.length ? `${files.length} 个文件` : '',
    removed.length ? `${removed.length} 个删除` : '',
  ]
    .filter(Boolean)
    .join(' · ');
  wrap.innerHTML = `
    <div class="artifact-summary-head">
      <span class="artifact-summary-title">📦 本轮变更</span>
      <span class="artifact-summary-sub">${esc(head || '无文件变更')}${kinds.length ? ` · ${esc(kinds.join(' / '))}` : ''}</span>
    </div>`;
  if (files.length) wrap.appendChild(artifactGrid(files));
  if (removed.length) wrap.appendChild(removedNode(removed));
  return wrap;
}

function parseArgs(raw) {
  if (!raw) return {};
  if (typeof raw === 'object') return raw;
  try {
    return JSON.parse(raw);
  } catch (_) {
    return { _raw: raw };
  }
}

function shortArgs(args) {
  const text = JSON.stringify(args || {});
  return text.length > 90 ? text.slice(0, 90) + '…' : text;
}

function buildToolCard(name, args, result) {
  const card = el('div', 'tool-card');
  if (result) card.classList.add('open');
  const parsed = parseArgs(args);
  const ok = result ? (result.stats ? result.stats.ok !== false : true) : null;
  const duration = result?.stats?.duration;
  const state = result
    ? `<span class="tool-state ${ok ? 'ok' : 'err'}">${ok ? '完成' : '失败'}${duration ? ' · ' + duration + 's' : ''}</span>`
    : '<span class="tool-state">运行中…</span>';
  const head = el('div', 'tool-head');
  head.innerHTML = `<span class="tool-ico">⚙</span><span class="tool-name">${esc(name)}</span><span class="tool-args">${esc(shortArgs(parsed))}</span>${state}`;
  head.onclick = () => card.classList.toggle('open');
  card.appendChild(head);

  const body = el('div', 'tool-body');
  body.innerHTML = `<div class="label">参数</div><pre>${esc(JSON.stringify(parsed, null, 2))}</pre>`;
  if (result) {
    body.appendChild(el('div', 'spacer'));
    const label = el('div', 'label');
    label.textContent = '返回';
    const pre = el('pre');
    pre.textContent = result.content || '';
    body.append(label, pre);
  }
  card.appendChild(body);
  card.dataset.callId = result?.tool_call_id || '';
  return card;
}

function statsNode(stats) {
  const parts = [];
  if (stats.ttft) parts.push(`首字 ${Number(stats.ttft).toFixed(2)}s`);
  if (stats.tps) parts.push(`${stats.tps} tok/s`);
  if (stats.tokens) parts.push(`${stats.tokens} tokens`);
  if (stats.reasoning_tokens) parts.push(`思考 ${stats.reasoning_tokens}`);
  if (stats.duration) parts.push(`耗时 ${Number(stats.duration).toFixed(1)}s`);
  if (stats.cache_hit && stats.reused_tokens) {
    parts.push(`缓存命中 ${Number(stats.reused_tokens).toLocaleString()} tok`);
  }
  if (stats.cancelled) parts.push('已停止');
  const node = el('div', 'stat');
  node.innerHTML = parts.map((p) => `<i>${esc(p)}</i>`).join('<i>·</i>');
  return node;
}

function actionsNode(msg) {
  const wrap = el('div', 'msg-actions');
  const copy = el('button', null, '复制');
  copy.onclick = async () => {
    await navigator.clipboard.writeText(msg.content || '');
    toast('已复制');
  };
  wrap.appendChild(copy);
  if (msg.role === 'assistant') {
    const regen = el('button', null, '重新生成');
    regen.onclick = () => regenerate(msg.id);
    wrap.appendChild(regen);
  }
  return wrap;
}

function scrollToBottom(force = false) {
  const scroller = $('#chatScroll');
  const near = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
  if (force || near) scroller.scrollTop = scroller.scrollHeight;
}

/* ---------------------------- 流式渲染 ---------------------------- */

let renderQueued = false;
function scheduleMdRender() {
  if (renderQueued || !S.live) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    const live = S.live;
    if (!live) return;
    live.mdEl.innerHTML = mdToHtml(live.raw) + '<span class="caret"></span>';
    enhance(live.mdEl);
    scrollToBottom();
  });
}

function startAssistant(round) {
  const node = el('div', 'msg assistant');
  node.innerHTML = `
    <div class="avatar">LM</div>
    <div class="body">
      <details class="think" hidden><summary>思考过程</summary><div class="think-body"></div></details>
      <div class="tool-stack"></div>
      <div class="md"></div>
      <div class="live-artifacts" hidden></div>
      <div class="loading-line"><span class="spinner"></span><span>${round > 0 ? '正在整理工具结果…' : '正在生成…'}</span></div>
      <div class="msg-foot"><div class="stat"></div><div class="msg-actions"></div></div>
    </div>`;
  $('#messages').appendChild(node);
  S.live = {
    node,
    raw: '',
    reasoning: '',
    mdEl: node.querySelector('.md'),
    stack: node.querySelector('.tool-stack'),
    think: node.querySelector('.think'),
    thinkBody: node.querySelector('.think-body'),
    artifacts: node.querySelector('.live-artifacts'),
    loading: node.querySelector('.loading-line'),
    stat: node.querySelector('.stat'),
    actions: node.querySelector('.msg-actions'),
    pending: {},
  };
  S.liveArtifacts = [];
  S.liveRemovals = [];
  $('#welcome').hidden = true;
  scrollToBottom(true);
  return S.live;
}

/* 文件变更时立刻在气泡里显示产物卡片（这轮结束后会由服务端清单重绘）。
   path 支持 `skills/` 前缀——技能库在工作目录之外，走 /api/skills/raw 通道。 */
async function addLiveArtifact(path) {
  const live = S.live;
  if (!live || !path) return;
  const p = String(path).replace(/\\/g, '/');
  if ((S.liveArtifacts || []).some((f) => f.path === p)) return;
  let file = null;
  if (p.startsWith('skills/')) {
    const name = p.split('/').pop();
    const kind = guessKind(name);
    const q = encodeURIComponent(p);
    file = {
      path: p,
      name,
      kind,
      lang: '',
      size: 0,
      url: `/api/skills/raw?path=${q}`,
      preview: `/api/skills/raw?path=${q}`,
      renderable: ['image', 'markdown', 'html', 'table', 'code', 'text'].includes(kind),
    };
  } else {
    try {
      const data = await api(withScope('/api/workspace/resolve'), { method: 'POST', body: { paths: [p] } });
      file = (data.files || [])[0] || null;
    } catch (_) {}
  }
  if (!file || (S.liveArtifacts || []).some((f) => f.path === file.path)) return;
  S.liveArtifacts = [...(S.liveArtifacts || []), file];
  renderLiveArtifacts(live);
  scrollToBottom();
}

function renderLiveArtifacts(live) {
  if (!live) return;
  if (!live.gridEl) {
    live.gridEl = el('div', 'artifact-grid');
    live.artifacts.appendChild(live.gridEl);
  }
  live.gridEl.replaceChildren(artifactGrid(S.liveArtifacts || []));
  live.artifacts.hidden = true;
  if ((S.liveArtifacts || []).length || (S.liveRemovals || []).length) {
    live.artifacts.hidden = false;
    if ((S.liveRemovals || []).length) {
      let list = live.artifacts.querySelector('.artifact-removed');
      if (!list) {
        list = el('div', 'artifact-removed');
        live.artifacts.appendChild(list);
      }
      list.innerHTML = (S.liveRemovals || [])
        .map((p) => `<span class="removed-chip" title="${esc(p)}">🗑 ${esc(shortPath(p))}（已删除）</span>`)
        .join('');
    }
  }
}

/* 监听器发现文件被删掉时，放一张灰色的「已删除」条目，点不了 */
function addLiveRemoval(path) {
  const live = S.live;
  if (!live || !path) return;
  const p = String(path).replace(/\\/g, '/');
  if ((S.liveRemovals || []).some((x) => x === p)) return;
  S.liveRemovals = [...(S.liveRemovals || []), p];
  renderLiveArtifacts(live);
  scrollToBottom();
}

function finishAssistant(stats, cancelled) {
  const live = S.live;
  if (!live) return;
  S.live = null;
  live.loading?.remove();
  const hasTools = live.stack.children.length > 0;
  if (live.raw) {
    live.mdEl.innerHTML = mdToHtml(live.raw);
    enhance(live.mdEl);
  } else if (hasTools) {
    live.mdEl.remove();
  } else if (live.reasoning) {
    live.mdEl.remove();
  } else {
    live.mdEl.innerHTML = '<p style="color:var(--text-dim)">（模型没有返回内容）</p>';
  }
  if (stats) {
    live.stat.innerHTML = statsNode({ ...stats, cancelled }).innerHTML;
  }
  if (live.reasoning) {
    live.think.hidden = false;
  }
  const msg = { content: live.raw };
  const copy = el('button', null, '复制');
  copy.onclick = async () => {
    await navigator.clipboard.writeText(live.raw);
    toast('已复制');
  };
  live.actions.appendChild(copy);
  const regen = el('button', null, '重新生成');
  regen.onclick = () => regenerate(live.node.dataset.id);
  live.actions.appendChild(regen);
  scrollToBottom();
}

/* ---------------------------- 发送 / 停止 ---------------------------- */

function clearQueueNote() {
  if (S.queueNote) {
    S.queueNote.remove();
    S.queueNote = null;
  }
}

function sysNote(text) {
  const node = el('div', 'sys-note');
  node.textContent = text;
  $('#messages').appendChild(node);
  scrollToBottom();
  return node;
}

function handleEvent(ev) {
  switch (ev.type) {
    case 'run_meta': {
      // 重连 / 重放：把这一轮已经落库的半成品消息先摘掉，交给事件流重新画
      const ids = ev.message_ids || [];
      if (ids.length) {
        S.messages = S.messages.filter((m) => !ids.includes(m.id));
        ids.forEach((id) => {
          const node = $(`#messages .msg[data-id="${id}"]`);
          if (node) node.remove();
        });
      }
      S.live = null;
      break;
    }
    case 'accepted':
      syncRunUi();
      break;
    case 'queue': {
      const text = `已排队，等待模型空闲（第 ${ev.position || 1} 位）…`;
      if (!S.queueNote) S.queueNote = sysNote(text);
      else S.queueNote.textContent = text;
      syncRunUi();
      break;
    }
    case 'user_injected': {
      // 排队消息在下一轮模型调用前自然插入：先收尾当前半截回答，再画用户气泡
      if (S.live) finishAssistant(null, false);
      $('#messages').appendChild(userNode({ id: ev.message_id, content: ev.content }));
      clearQueueNote();
      scrollToBottom();
      loadQueue();
      break;
    }
    case 'running':
      clearQueueNote();
      syncRunUi();
      break;
    case 'start':
      clearQueueNote();
      if (ev.usage) {
        S.ctx = ev.usage;
        updateCtxMeter();
      }
      syncRunUi();
      break;
    case 'context':
      if (ev.usage) {
        S.ctx = ev.usage;
        updateCtxMeter();
      }
      break;
    case 'compaction': {
      if (ev.phase === 'start') {
        S.compactNote = sysNote(
          ev.reason === 'context_overflow'
            ? '上下文超出模型上限，正在压缩较早历史后重试…'
            : '正在压缩较早的上下文…'
        );
      } else if (ev.phase === 'done') {
        const note = S.compactNote || sysNote('');
        note.textContent = `已压缩 ${ev.covered || 0} 条较早消息，节省上下文`;
        S.compactNote = null;
        if (ev.usage) {
          S.ctx = ev.usage;
          updateCtxMeter();
        }
      } else if (ev.phase === 'error' || ev.phase === 'skipped') {
        (S.compactNote || sysNote('')).textContent = `上下文压缩未执行：${ev.message || '历史不足'}`;
        S.compactNote = null;
      }
      break;
    }
    case 'thinking_done': {
      if (S.live) {
        const summary = S.live.think.querySelector('summary');
        if (summary && !summary.querySelector('.think-note')) {
          summary.insertAdjacentHTML('beforeend', '<span class="think-note">已达思考预算，转入回答</span>');
        }
      }
      break;
    }
    case 'message_start':
      // 上下文超限重试：沿用同一个气泡，把已经画出来的半成品清掉重新生成
      if (ev.reset && S.live && S.live.node.dataset.id === ev.message_id) {
        S.live.raw = '';
        S.live.reasoning = '';
        S.live.mdEl.innerHTML = '';
        S.live.thinkBody.textContent = '';
        S.live.think.hidden = true;
        S.live.stat.textContent = '';
        break;
      }
      if (S.live) finishAssistant(null, false);
      startAssistant(ev.round || 0);
      if (S.live) S.live.node.dataset.id = ev.message_id;
      break;
    case 'meta':
      break;
    case 'delta': {
      if (!S.live) startAssistant(0);
      S.live.raw += ev.text;
      S.live.loading?.remove();
      scheduleMdRender();
      break;
    }
    case 'reasoning': {
      if (!S.live) startAssistant(0);
      S.live.reasoning += ev.text;
      S.live.think.hidden = false;
      S.live.think.open = true;
      S.live.thinkBody.textContent = S.live.reasoning;
      scrollToBottom();
      break;
    }
    case 'tool_call': {
      if (!S.live) startAssistant(0);
      const card = buildToolCard(ev.name, ev.arguments, null);
      card.dataset.callId = ev.id;
      S.live.stack.appendChild(card);
      S.live.pending[ev.id] = card;
      scrollToBottom();
      break;
    }
    case 'tool_result': {
      const card = S.live?.pending?.[ev.call_id] || S.live?.node.querySelector(`[data-call-id="${ev.call_id}"]`);
      if (card) {
        const args = parseArgs(card.querySelector('pre')?.textContent || '{}');
        const replacement = buildToolCard(ev.name, args, {
          content: ev.content,
          tool_call_id: ev.call_id,
          stats: { ok: ev.ok, duration: ev.duration },
        });
        replacement.classList.add('open');
        card.replaceWith(replacement);
        delete S.live.pending[ev.call_id];
        // 写文件的工具一返回就先把产物卡片摆出来，不用等整轮结束
        if (PRODUCE_TOOLS.includes(ev.name) && args.path) addLiveArtifact(String(args.path));
      }
      scrollToBottom();
      if (
        S.workspace.open &&
        ['write_file', 'edit_file', 'delete_file', 'run_python', 'run_command', 'make_dir'].includes(ev.name)
      ) {
        loadWorkspace();
      }
      break;
    }
    case 'file_changes': {
      // 运行期间监听到的所有文件变更：新增/修改 → 产物卡片，删除 → 灰色条目
      const changes = ev.changes || [];
      changes.forEach((ch) => {
        if (!ch || !ch.path) return;
        if (ch.action === 'deleted') addLiveRemoval(ch.path);
        else addLiveArtifact(ch.path);
      });
      if (changes.length && S.workspace.open) loadWorkspace();
      // 技能库被改了（skill_create / skill_delete）→ 稍后刷新候选列表
      if (changes.some((ch) => String(ch.path || '').startsWith('skills/'))) {
        S.skillsDirty = true;
        refreshSkillsSoon(600);
      }
      break;
    }
    case 'notice':
      toast(ev.message || '已自动降级处理', 4200);
      if (ev.message) sysNote(ev.message);
      break;
    case 'stats':
      if (S.live) S.live.stat.innerHTML = statsNode(ev.stats).innerHTML;
      break;
    case 'error':
      toast(ev.message || '生成失败', 3200);
      if (S.live && !S.live.raw) {
        S.live.raw = `⚠️ ${ev.message || '生成失败'}`;
      }
      break;
    case 'stopping':
      if (S.activeId) S.stopping[S.activeId] = true;
      syncRunUi();
      break;
    case 'done':
      if (S.activeId) delete S.stopping[S.activeId];
      finishAssistant(null, ev.cancelled);
      syncRunUi();
      if (S.skillsDirty) {
        S.skillsDirty = false;
        refreshSkillsSoon(0);
      }
      break;
    case 'end':
      if (S.activeId) delete S.stopping[S.activeId];
      if (S.live) finishAssistant(null, false);
      syncRunUi();
      if (S.skillsDirty) {
        S.skillsDirty = false;
        refreshSkillsSoon(0);
      }
      break;
  }
}

async function readSSE(res, onEvent) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      chunk.split('\n').forEach((line) => {
        if (!line.startsWith('data:')) return;
        const raw = line.slice(5).trim();
        if (!raw) return;
        try {
          onEvent(JSON.parse(raw));
        } catch (err) {
          console.warn('bad event', raw, err);
        }
      });
    }
  }
}

async function send(text) {
  const content = (text ?? $('#input').value).trim();
  const attachments = S.pendingAttachments.slice();
  if (!content && !attachments.length) return;
  if (S.pendingUploading) {
    toast('附件还在上传，稍等一下');
    return;
  }

  if (!S.conv) await createConversation();
  const conv = S.conv;
  $('#input').value = '';
  closeSlashMenu();
  autoGrow($('#input'));
  clearPendingAttachments();

  // 正在生成 → 进队列，不丢消息也不打断当前这轮
  if (runOf(conv.id)) {
    try {
      await api(`/api/conversations/${conv.id}/queue`, {
        method: 'POST',
        body: { content, attachments },
      });
      await loadQueue();
      await syncRuns();
      toast('已加入队列，这轮结束后自动发送');
    } catch (err) {
      toast(`入队失败：${err.message}`, 3000);
    }
    return;
  }

  await startRun(conv.id, { content, attachments });
}

async function startRun(convId, { content = '', regenerate = false, attachments = [] } = {}) {
  const runId = `run_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`;
  S.runs[convId] = { id: runId, status: 'running', source: 'chat', conversation_id: convId };
  S.runSeq[runId] = 0;
  delete S.stopping[convId];
  clearQueueNote();

  if (S.activeId === convId && !regenerate) {
    $('#messages').appendChild(userNode({ content, id: 'tmp', attachments }));
    $('#welcome').hidden = true;
    S.messages.push({ role: 'user', content, attachments, id: 'tmp' });
    scrollToBottom(true);
  }
  renderSidebar();
  syncRunUi();
  detachStream();
  const ctrl = new AbortController();
  S.attach = { runId, convId, ctrl };

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: convId,
        content,
        attachments,
        regenerate,
        request_id: runId,
        model_id: S.conv?.model_id || $('#modelSelect').value,
        thinking: !!S.conv?.thinking,
        thinking_level: S.conv?.thinking_level || 'medium',
        skills: S.conv?.active_skills || [],
      }),
      signal: ctrl.signal,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    await readSSE(res, (ev) => {
      if (S.activeId !== convId) return;
      if (ev.seq) S.runSeq[runId] = ev.seq;
      handleEvent(ev);
    });
  } catch (err) {
    if (err.name !== 'AbortError') {
      toast(`请求失败：${err.message}`, 3600);
      if (S.live) finishAssistant(null, true);
    }
  } finally {
    if (S.attach?.ctrl === ctrl) S.attach = null;
    if (S.activeId === convId) {
      if (S.live) finishAssistant(null, false);
      await refreshAfterTurn();
    } else {
      await loadConversations();
    }
    delete S.runs[convId];
    syncRunUi();
  }
}

async function refreshAfterTurn() {
  await loadConversations();
  clearQueueNote();
  if (S.activeId) {
    const data = await api(`/api/conversations/${S.activeId}/messages`);
    S.conv = data.conversation;
    S.messages = data.messages || [];
    // 流式结束后重绘，保证消息 id / 状态与数据库一致
    await renderChat();
    refreshContext();
    await loadQueue();
    if (!$('#usagePage').hidden) loadUsage();
  }
  updateHeader();
  if (S.workspace.open) loadWorkspace();
}

async function stop() {
  const run = runOf(S.activeId);
  if (!run) return;
  S.stopping[S.activeId] = true;
  syncRunUi();
  try {
    let res = await api(`/api/runs/${run.id}/stop`, { method: 'POST' });
    if (!res.ok) {
      // 刚点发送的那一瞬间，运行可能还没在后端登记：稍等重试一次
      await new Promise((r) => setTimeout(r, 350));
      res = await api(`/api/runs/${run.id}/stop`, { method: 'POST' });
    }
    if (res.ok) toast('已停止');
    else toast('这一轮已经结束了', 2200);
  } catch (err) {
    toast(`停止失败：${err.message}`, 3000);
  } finally {
    setTimeout(() => {
      if (!busyHere()) delete S.stopping[S.activeId];
      syncRunUi();
      syncRuns();
    }, 700);
  }
}

async function regenerate(messageId) {
  if (!S.conv) return;
  if (runOf(S.conv.id)) return toast('正在生成，等这轮结束再重新生成');
  // 移除被截断的尾部消息节点
  const nodes = $$('#messages .msg');
  let hit = false;
  [...nodes].reverse().forEach((n) => {
    if (hit) n.remove();
    if (n.dataset.id === messageId) hit = true;
  });
  await startRun(S.conv.id, { regenerate: true });
}

/* ---------------------------- 消息队列 ---------------------------- */

async function loadQueue() {
  if (!S.activeId) {
    S.queue = [];
    renderQueue();
    return;
  }
  try {
    const data = await api(`/api/conversations/${S.activeId}/queue`);
    S.queue = data.items || [];
  } catch (_) {
    S.queue = [];
  }
  renderQueue();
}

async function queueMutate(fn) {
  try {
    await fn();
  } catch (err) {
    toast(`队列操作失败：${err.message}`, 2800);
  }
  await loadQueue();
  await syncRuns();
}

function renderQueue() {
  const box = $('#queuePanel');
  if (!box) return;
  const items = S.queue || [];
  box.innerHTML = '';
  box.hidden = items.length === 0;
  if (!items.length) return;
  const head = el('div', 'queue-head');
  head.innerHTML = `<span><b>排队 ${items.length}</b> · 本轮结束后按顺序发送，「插队」会在下一次模型调用前插入</span>`;
  const clear = el('button', 'mini-btn', '清空');
  clear.onclick = () =>
    queueMutate(async () => {
      for (const item of items) await api(`/api/queue/${item.id}`, { method: 'DELETE' });
    });
  head.appendChild(clear);
  box.appendChild(head);
  items.forEach((item, idx) => box.appendChild(queueRow(item, idx, items.length)));
}

function queueRow(item, idx, total) {
  const row = el('div', 'queue-item');
  const order = el('div', 'queue-order', String(idx + 1));
  const text = el('div', 'queue-text');
  const files = item.attachments || [];
  const label = item.content || (files.length ? files.map((f) => f.name).join('、') : '');
  text.textContent = files.length ? `${label}　📎${files.length}` : label;
  text.title = '点击编辑';
  text.onclick = () => startQueueEdit(row, item);

  const mk = (label, title, handler, cls) => {
    const btn = el('button', 'queue-btn' + (cls ? ' ' + cls : ''), label);
    btn.title = title;
    btn.onclick = (e) => {
      e.stopPropagation();
      handler();
    };
    return btn;
  };

  const actions = el('div', 'queue-actions');
  const hasFiles = (item.attachments || []).length > 0;
  actions.append(
    mk('↑', '上移', () => moveQueueItem(idx, -1)),
    mk('↓', '下移', () => moveQueueItem(idx, 1)),
    mk('插队', hasFiles
      ? '带附件的消息无法中途插入，将移到队首、本轮结束后优先发送'
      : '移到队首，在当前这轮的下一次模型调用前自然插入（不打断执行）', () =>
      queueMutate(async () => {
        await api(`/api/queue/${item.id}/now`, { method: 'POST' });
      }), 'go'),
    mk('✕', '删除', () =>
      queueMutate(async () => {
        await api(`/api/queue/${item.id}`, { method: 'DELETE' });
      }), 'del'),
  );
  if (idx === 0) actions.children[0].disabled = true;
  if (idx === total - 1) actions.children[1].disabled = true;
  row.append(order, text, actions);
  return row;
}

function startQueueEdit(row, item) {
  row.innerHTML = '';
  const area = el('textarea', 'queue-edit');
  area.value = item.content;
  area.rows = 2;
  const bar = el('div', 'queue-edit-bar');
  const save = el('button', 'queue-btn go', '保存');
  const cancel = el('button', 'queue-btn', '取消');
  save.onclick = () =>
    queueMutate(async () => {
      await api(`/api/queue/${item.id}`, { method: 'PATCH', body: { content: area.value } });
    });
  cancel.onclick = () => renderQueue();
  area.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      save.click();
    }
    if (e.key === 'Escape') cancel.click();
  });
  bar.append(save, cancel);
  row.append(area, bar);
  area.focus();
  area.setSelectionRange(area.value.length, area.value.length);
}

async function moveQueueItem(idx, delta) {
  const items = (S.queue || []).slice();
  const target = idx + delta;
  if (target < 0 || target >= items.length) return;
  [items[idx], items[target]] = [items[target], items[idx]];
  await queueMutate(async () => {
    await api(`/api/conversations/${S.activeId}/queue/reorder`, {
      method: 'POST',
      body: { ids: items.map((i) => i.id) },
    });
  });
}

/* ---------------------------- 定时任务 ---------------------------- */

function fmtNext(ts) {
  if (!ts) return '不再触发';
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const sameDay = d.toDateString() === new Date().toDateString();
  const day = `${d.getMonth() + 1}/${d.getDate()}`;
  return `${sameDay ? '今天' : day} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const SCHED_STATUS = {
  running: '执行中',
  done: '已完成',
  stopped: '已停止',
  error: '失败',
  busy_retry: '会话忙，稍后重试',
  empty_prompt: '内容为空',
};

/* ---------------- 整页视图（定时任务 / Token 用量） ---------------- */

/** 主区一次只显示一个整页视图，聊天区让位 */
function openPage(id, view) {
  document.querySelector('.main').classList.add('page-open');
  $$('.page-view').forEach((node) => {
    node.hidden = node.id !== id;
  });
  $('#openUsage')?.classList.toggle('on', id === 'usagePage');
  $('#navSchedules')?.classList.toggle('on', id === 'schedPage');
  S.view = view;
}

function closePage() {
  $$('.page-view').forEach((node) => {
    node.hidden = true;
  });
  document.querySelector('.main').classList.remove('page-open');
  $('#openUsage')?.classList.remove('on');
  $('#navSchedules')?.classList.remove('on');
  S.view = 'chat';
}

async function openSchedules() {
  closeAllPopovers();
  openPage('schedPage', 'schedules');
  $('#schedEditor').hidden = true;
  if (!$('#schAt').value) {
    $('#schAt').value = toLocalInput(Math.floor(Date.now() / 1000) + 3600);
  }
  await loadSchedules();
  scheduleKindChanged();
}

function closeSchedules() {
  closePage();
  renderChat();
  updateHeader();
}

function toggleScheduleEditor(show = $('#schedEditor').hidden) {
  $('#schedEditor').hidden = !show;
  if (show) {
    scheduleKindChanged();
    $('#schNl').focus();
  }
}

async function loadSchedules() {
  try {
    const data = await api('/api/schedules');
    S.schedules = data.schedules || [];
    const kinds = data.kinds || [];
    const sel = $('#schKind');
    if (sel && sel.options.length !== kinds.length) {
      sel.innerHTML = kinds.map((k) => `<option value="${k.value}">${k.label}</option>`).join('');
    }
    const wd = $('#schWeekday');
    if (wd && !wd.options.length) {
      wd.innerHTML = (data.weekdays || ['周一'])
        .map((label, i) => `<option value="${i}">${label}</option>`)
        .join('');
    }
    renderSchedules();
    renderSidebar();
  } catch (err) {
    toast(`读取定时任务失败：${err.message}`, 3000);
  }
}

function renderSchedules() {
  const list = $('#scheduleList');
  if (!list) return;
  list.innerHTML = '';
  if (!S.schedules.length) {
    list.innerHTML = `<div class="sched-empty">
      <div class="sched-empty-ico">⏰</div>
      <p>还没有定时任务</p>
      <span>点右上角「新建任务」，或者直接在对话里说「每天 9 点帮我整理日报」。</span>
    </div>`;
    return;
  }
  S.schedules.forEach((s) => list.appendChild(scheduleCard(s)));
}

function scheduleCard(s) {
  const space = s.space || {};
  const path = space.path || '';
  const card = el('div', `sched-card${s.enabled ? '' : ' off'}`);
  const status = s.last_status ? SCHED_STATUS[s.last_status] || s.last_status : '';
  card.innerHTML = `
    <div class="sched-card-head">
      <span class="sched-ico">${esc(space.icon || '⏰')}</span>
      <div class="sched-title-wrap">
        <div class="sched-title">${esc(s.title || '未命名任务')}</div>
        <div class="sched-meta">
          <span>${esc(s.summary || '')}</span>
          <span>下次 ${esc(fmtNext(s.next_run_at))}</span>
          <span>已跑 ${s.run_count || 0} 次</span>
          ${status ? `<span class="sched-status ${esc(s.last_status)}">${esc(status)}</span>` : ''}
        </div>
      </div>
      <div class="sched-actions">
        <button class="mini-btn" data-act="run">运行</button>
        <button class="mini-btn" data-act="toggle">${s.enabled ? '停用' : '启用'}</button>
        <button class="mini-btn danger" data-act="del" title="删除任务">✕</button>
      </div>
    </div>
    <p class="sched-prompt">${esc((s.prompt || '').slice(0, 220))}</p>
    <div class="sched-dir">
      <span class="sched-dir-name">📁 ${esc(space.name || s.title || '目录')}</span>
      <span class="sched-dir-path" title="${esc(path)}">${esc(shortPath(path))}</span>
      <button class="mini-btn" data-act="reveal">打开目录</button>
      <button class="mini-btn" data-act="clear" title="删除这个任务的所有执行记录">清空记录</button>
    </div>
    <div class="sched-runs">
      ${
        (s.runs || []).length
          ? (s.runs || [])
              .map(
                (run) => `<button class="run-chip" data-run="${run.id}" title="${esc(run.title)}">
                  <span class="run-dot-sm"></span>${esc(fmtShortTime(run.updated_at))}
                  <span class="run-count">${run.message_count || 0}</span>
                </button>`
              )
              .join('')
          : '<span class="sched-runs-empty">还没有执行记录</span>'
      }
      ${(s.run_total || 0) > 12 ? `<span class="sched-runs-empty">共 ${s.run_total} 次</span>` : ''}
    </div>`;

  card.querySelector('[data-act="run"]').onclick = () => runScheduleNow(s.id);
  card.querySelector('[data-act="toggle"]').onclick = async () => {
    await api(`/api/schedules/${s.id}`, { method: 'PATCH', body: { enabled: !s.enabled } });
    await loadSchedules();
  };
  card.querySelector('[data-act="del"]').onclick = async () => {
    if (!confirm(`删除定时任务「${s.title || s.prompt}」？\n目录和历史对话会保留下来，变成一个普通空间。`)) return;
    await api(`/api/schedules/${s.id}`, { method: 'DELETE' });
    await loadSchedules();
    await loadSpaces();
    toast('已删除');
  };
  card.querySelector('[data-act="reveal"]').onclick = async () => {
    try {
      await api('/api/spaces/reveal', { method: 'POST', body: { path } });
    } catch (err) {
      toast(`打开失败：${err.message}`);
    }
  };
  card.querySelector('[data-act="clear"]').onclick = async () => {
    const runs = s.runs || [];
    if (!runs.length) return toast('还没有执行记录');
    if (!confirm(`清空「${s.title}」的 ${s.run_total || runs.length} 条执行记录？只删对话，目录里的文件保留。`)) return;
    for (const run of runs) {
      try {
        await api(`/api/conversations/${run.id}`, { method: 'DELETE' });
      } catch (_) {}
    }
    await loadSchedules();
    await loadConversations();
    toast('已清空记录');
  };
  card.querySelectorAll('.run-chip').forEach((chip) => {
    chip.onclick = async () => {
      closeSchedules();
      await loadConversations();
      await openConversation(chip.dataset.run);
    };
  });
  return card;
}

async function runScheduleNow(id) {
  toast('正在触发…');
  try {
    const res = await api(`/api/schedules/${id}/run`, { method: 'POST' });
    toast(res.ok ? '已开始执行，结果会出现在任务目录下' : res.detail || '触发失败', 3000);
    await loadSchedules();
    await syncRuns();
  } catch (err) {
    toast(`触发失败：${err.message}`, 3000);
  }
}

function scheduleKindChanged() {
  const kind = $('#schKind').value;
  $$('[data-sch-kind]').forEach((node) => {
    node.hidden = !node.dataset.schKind.split(',').includes(kind);
  });
}

async function parseScheduleText() {
  const text = $('#schNl').value.trim();
  if (!text) return toast('先写一句，例如「每天 9:00 整理昨天的日报」');
  try {
    const parsed = await api('/api/schedules/parse', { method: 'POST', body: { text } });
    if (!parsed.ok) return toast(parsed.hint || '没看懂时间规则', 3600);
    $('#schKind').value = parsed.kind;
    scheduleKindChanged();
    if (parsed.prompt) {
      $('#schPrompt').value = parsed.prompt;
      $('#schTitle').value = parsed.prompt.slice(0, 16);
    }
    if (parsed.kind === 'every') $('#schEvery').value = parsed.every_minutes || 60;
    if (parsed.time_of_day) $('#schTime').value = parsed.time_of_day;
    if (parsed.weekday != null) $('#schWeekday').value = String(parsed.weekday);
    if (parsed.kind === 'once' && parsed.at) $('#schAt').value = toLocalInput(parsed.at);
    toast(`识别为「${parsed.summary}」，确认后点保存`, 2600);
  } catch (err) {
    toast(`解析失败：${err.message}`, 3000);
  }
}

function toLocalInput(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

async function saveSchedule() {
  const kind = $('#schKind').value;
  const body = {
    title: $('#schTitle').value.trim(),
    prompt: $('#schPrompt').value.trim(),
    kind,
  };
  if (kind === 'once') {
    const raw = $('#schAt').value;
    if (!raw) return toast('选一个执行时间');
    body.at = new Date(raw).getTime() / 1000;
  } else if (kind === 'every') {
    body.every_minutes = Number($('#schEvery').value) || 60;
  } else {
    body.time_of_day = $('#schTime').value || '09:00';
    if (kind === 'weekly') body.weekday = Number($('#schWeekday').value) || 0;
  }
  try {
    await api('/api/schedules', { method: 'POST', body });
    $('#schTitle').value = '';
    $('#schPrompt').value = '';
    $('#schNl').value = '';
    toast('定时任务已创建，目录也建好了');
    toggleScheduleEditor(false);
    await loadSchedules();
  } catch (err) {
    toast(`创建失败：${err.message}`, 3600);
  }
}

/* ---------------------------- 模型状态 ---------------------------- */

async function loadModels(probe = false) {
  const data = await api(`/api/models${probe ? '?probe=1' : ''}`);
  S.models = data.models || [];
  renderModelSelect();
  renderModelCard();
  renderModelList();
}

function renderModelSelect() {
  const select = $('#modelSelect');
  const current = S.conv?.model_id || S.config?.active_model_id;
  const groups = new Map();
  S.models.forEach((m) => {
    const key =
      m.provider === 'local_hf' ? '本地权重' : m.connection_label || '未归类';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(m);
  });
  select.innerHTML = [...groups.entries()]
    .map(([label, list]) => {
      const opts = list
        .map((m) => `<option value="${m.id}">${esc(m.label)}</option>`)
        .join('');
      return `<optgroup label="${esc(label)}">${opts}</optgroup>`;
    })
    .join('');
  if (current) select.value = current;
  updateModelChip();
  renderModelMenu(groups);
}

function updateModelChip() {
  const chip = $('#modelChip');
  if (!chip) return;
  const id = $('#modelSelect')?.value || S.conv?.model_id || S.config?.active_model_id || '';
  const model = S.models.find((m) => m.id === id);
  $('#modelChipName').textContent = model?.label || (S.models.length ? '选择模型' : '未配置模型');
  chip.classList.toggle('on', !!model);
  chip.title = model ? `当前模型：${model.label} · 点击切换` : '选择模型';
}

function renderModelMenu(groups) {
  const box = $('#modelMenuList');
  if (!box) return;
  const current = $('#modelSelect')?.value || S.conv?.model_id || S.config?.active_model_id || '';
  box.innerHTML = '';
  if (!S.models.length) {
    box.innerHTML = '<div class="model-empty">还没有可用模型<br/>点上方「管理」去添加</div>';
    return;
  }
  [...groups.entries()].forEach(([label, list]) => {
    box.appendChild(el('div', 'model-group', esc(label)));
    list.forEach((m) => {
      const row = el('button', `model-row${m.id === current ? ' active' : ''}`);
      row.innerHTML = `
        <span class="mr-dot"></span>
        <span class="mr-name">${esc(m.label)}</span>
        <span class="mr-tag">${m.provider === 'local_hf' ? '本地' : 'API'}</span>
      `;
      row.onclick = async () => {
        closeAllPopovers();
        if (m.id === current) return;
        try {
          await useModel(m.id);
          toast(`已切换到 ${m.label}`);
        } catch (err) {
          toast(`切换失败：${err.message}`, 3200);
        }
      };
      box.appendChild(row);
    });
  });
}

function closeAllPopovers() {
  ['modelMenu', 'thinkMenu', 'skillMenu', 'slashMenu'].forEach((id) => {
    const node = $(`#${id}`);
    if (node) node.hidden = true;
  });
  slash.open = false;
}

function activeModel() {
  const id = S.conv?.model_id || S.config?.active_model_id;
  return S.models.find((m) => m.id === id);
}

function renderModelCard() {
  const model = activeModel();
  const dot = $('#modelDot');
  dot.className = 'status-dot';
  const status = model?.status || {};
  const state = status.state || 'unknown';
  if (state === 'ready') dot.classList.add('ready');
  else if (state === 'loading') dot.classList.add('loading');
  else if (state === 'error' || state === 'offline') dot.classList.add('error');

  $('#modelCardName').textContent = model?.label || '未选择模型';
  const detailMap = {
    ready: status.detail || '已就绪',
    loading: '加载中…',
    unloaded: status.path_exists === false ? '模型目录不存在' : '未加载（首次对话自动加载）',
    error: status.detail || '加载失败',
    offline: status.detail || '服务未启动',
    unknown: status.detail || '未探测',
  };
  $('#modelCardDetail').textContent = detailMap[state] || state;

  const btn = $('#modelCardBtn');
  if (model?.provider === 'local_hf') {
    btn.textContent = state === 'ready' ? '卸载' : state === 'loading' ? '加载中' : '加载';
    btn.disabled = state === 'loading';
  } else {
    btn.textContent = '测试';
    btn.disabled = false;
  }
}

async function toggleModel() {
  const model = activeModel();
  if (!model) return;
  const btn = $('#modelCardBtn');
  btn.disabled = true;
  try {
    if (model.provider === 'local_hf') {
      const ready = model.status?.state === 'ready';
      btn.textContent = ready ? '卸载中…' : '加载中…';
      await api(`/api/models/${model.id}/${ready ? 'unload' : 'load'}`, { method: 'POST' });
      toast(ready ? '模型已卸载' : '模型已就绪');
    } else {
      await loadModels(true);
      const fresh = activeModel();
      toast(fresh?.status?.state === 'ready' ? `连接正常：${fresh.status.detail}` : `连接失败：${fresh?.status?.detail || ''}`, 2600);
    }
  } catch (err) {
    toast(`操作失败：${err.message}`, 3200);
  } finally {
    btn.disabled = false;
    await loadModels();
  }
}

/* ---------------------------- 设置弹窗 ---------------------------- */

function openModal(id) {
  $(`#${id}`).hidden = false;
}
function closeModal(id) {
  $(`#${id}`).hidden = true;
  // 弹窗关掉后，浮层里可能还留着没关的下拉面板
  if (typeof PICKERS !== 'undefined') {
    Object.keys(PICKERS).forEach((which) => closePickerPanel(which));
  }
}

async function openSettings(tab = 'models') {
  const cfg = S.config;
  $('#prefSystem').value = cfg.defaults.system_prompt || '';
  $('#prefTemp').value = cfg.defaults.temperature;
  $('#prefTempVal').textContent = Number(cfg.defaults.temperature).toFixed(2);
  $('#prefTopP').value = cfg.defaults.top_p;
  $('#prefTopPVal').textContent = Number(cfg.defaults.top_p).toFixed(2);
  $('#prefMaxTokens').value = cfg.defaults.max_tokens;
  const roundsLimit = cfg.tools.max_rounds_limit || 200;
  const roundsInput = $('#prefMaxRounds');
  roundsInput.max = String(roundsLimit);
  roundsInput.value = Number(cfg.tools.max_rounds) || 50;
  $('#prefTools').checked = !!cfg.tools.enabled;
  $('#prefShell').checked = cfg.tools.allow_shell !== false;
  $('#prefAutoCompact').checked = cfg.defaults.auto_compact !== false;
  $('#prefCompactMode').value = cfg.defaults.compact_mode || 'tokens';
  $('#prefCompactTokens').value = cfg.defaults.compact_tokens ?? 500000;
  $('#prefCompactOnOverflow').checked = cfg.defaults.compact_on_overflow !== false;
  $('#prefThreshold').value = cfg.defaults.compact_threshold ?? 0.75;
  $('#prefThresholdVal').textContent = Number(cfg.defaults.compact_threshold ?? 0.75).toFixed(2);
  $('#prefKeepRecent').value = cfg.defaults.compact_keep_recent ?? 8;
  $('#wsMaxRead').value = cfg.workspace?.max_read_bytes || 200000;
  $('#wsMaxTree').value = cfg.workspace?.max_tree_entries || 400;
  $('#mcpPath').textContent = cfg.paths?.mcp || '';
  $('#skillsPath').textContent = cfg.paths?.skills || '';
  $('#memoryPath').textContent = cfg.paths?.memory || '';
  fillPerfForm(cfg.perf);
  $('#mcpInline').checked = !!(cfg.mcp && cfg.mcp.inline_tools);
  loadHub();

  await loadTools();
  renderToolGrid();
  renderModelList();
  await Promise.all([loadSkills(), loadMcp(), loadMemory()]);
  renderSkillList();
  renderMcpList();
  renderMemory();
  loadWorkspaceSummary();
  switchTab(tab);
  openModal('settingsModal');
  loadSystemInfo();
  loadPerf();
}

async function refreshSettingsTab(tab) {
  if (tab === 'mcp') {
    await Promise.all([loadMcp(), loadTools()]);
    renderMcpList();
    renderToolGrid();
  } else if (tab === 'skills') {
    await loadSkills();
    renderSkillList();
  } else if (tab === 'memory') {
    await loadMemory();
    renderMemory();
  } else if (tab === 'workspace') {
    renderSpaceManage();
    loadWorkspaceSummary();
  } else if (tab === 'models') {
    await loadModels();
    renderModelList();
  } else if (tab === 'perf') {
    await loadPerf();
  }
}

function switchTab(tab) {
  $$('#settingsTabs .tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === tab));
  $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === tab));
}

function fmtCtx(n) {
  const num = Number(n) || 0;
  return num >= 1000 ? `${Math.round(num / 1000)}k` : String(num);
}

function renderModelList() {
  const box = $('#modelList');
  if (!box) return;
  box.innerHTML = '';

  const conns = S.config?.connections || [];
  const byConn = new Map();
  const local = [];
  S.models.forEach((m) => {
    if (m.provider === 'local_hf') {
      local.push(m);
      return;
    }
    const cid = m.connection_id || '';
    if (!byConn.has(cid)) byConn.set(cid, []);
    byConn.get(cid).push(m);
  });

  if (local.length) {
    box.appendChild(
      modelGroupCard({
        title: '本地权重',
        sub: '进程内 transformers 推理，不需要地址和密钥',
        tag: '本地',
        models: local,
        addLabel: '+ 添加本地模型',
        onAdd: () => openModelEditor(null, 'local_hf'),
      })
    );
  }

  conns.forEach((conn) => {
    const models = byConn.get(conn.id) || [];
    box.appendChild(
      modelGroupCard({
        title: conn.label || conn.id,
        sub: `${conn.base_url || '（还没填地址）'}${conn.has_api_key ? ' · 密钥已保存' : ' · 无密钥'}`,
        tag: 'API',
        models,
        actions: [
          { label: '获取模型', act: 'probe' },
          { label: '编辑', act: 'edit' },
          { label: '删除', act: 'del', danger: true },
        ],
        onAction: (act) => {
          if (act === 'edit') return openConnEditor(conn);
          if (act === 'del') return deleteConnection(conn);
          return probeConnFromList(conn);
        },
        addLabel: '+ 在此连接下添加模型',
        onAdd: () => openModelEditor(null, 'openai_compat', conn.id),
      })
    );
  });

  const orphans = byConn.get('') || [];
  if (orphans.length) {
    box.appendChild(
      modelGroupCard({
        title: '未归类的模型',
        sub: '这些模型没有关联到连接，补一下连接信息才能用',
        tag: '待配置',
        models: orphans,
        addLabel: '+ 添加模型',
        onAdd: () => openModelEditor(null, 'openai_compat'),
      })
    );
  }
}

function modelGroupCard({ title, sub, tag, models, actions, onAction, addLabel, onAdd }) {
  const card = el('div', 'conn-card');
  const head = el('div', 'conn-head');
  const state = models[0]?.status?.state || 'unknown';
  const dotClass =
    state === 'ready' ? 'ready' : state === 'loading' ? 'loading' : state === 'error' || state === 'offline' ? 'error' : '';
  head.innerHTML = `
    <span class="status-dot ${dotClass}"></span>
    <div class="meta">
      <div class="name">${esc(title)}
        <span class="tag gray">${models.length} 个模型</span>
        ${tag ? `<span class="tag gray">${esc(tag)}</span>` : ''}
      </div>
      <div class="sub">${esc(sub)}</div>
    </div>
    <div class="ops">${(actions || [])
      .map((a) => `<button data-act="${a.act}" class="${a.danger ? 'danger' : ''}">${a.label}</button>`)
      .join('')}</div>`;
  (actions || []).forEach((a) => {
    head.querySelector(`[data-act="${a.act}"]`).onclick = () => onAction?.(a.act);
  });
  card.appendChild(head);

  const listBox = el('div', 'conn-models');
  models.forEach((m) => listBox.appendChild(modelRowEl(m)));
  const add = el('button', 'model-add', addLabel);
  add.onclick = onAdd;
  listBox.appendChild(add);
  card.appendChild(listBox);
  return card;
}

function modelRowEl(model) {
  const status = model.status || {};
  const state = status.state || 'unknown';
  const dotClass =
    state === 'ready' ? 'ready' : state === 'loading' ? 'loading' : state === 'error' || state === 'offline' ? 'error' : '';
  const row = el('div', 'model-row');
  const sub =
    model.provider === 'local_hf'
      ? status.model_path || ''
      : `${model.model || '—'} · 上下文 ${fmtCtx(model.context_window)}`;
  row.innerHTML = `
    <span class="status-dot ${dotClass}"></span>
    <div class="meta">
      <div class="name">${esc(model.label || model.model || model.id)}
        ${model.active ? '<span class="tag">当前</span>' : ''}
        ${model.supports_vision ? '<span class="tag gray">可看图</span>' : ''}
      </div>
      <div class="sub">${esc(sub)}${status.detail ? ' · ' + esc(status.detail) : ''}</div>
    </div>
    <div class="ops">
      ${
        model.provider === 'local_hf'
          ? `<button data-act="toggle">${state === 'ready' ? '卸载' : '加载'}</button>`
          : `<button data-act="use" ${model.active ? 'disabled' : ''}>${model.active ? '使用中' : '使用'}</button>`
      }
      <button data-act="edit">编辑</button>
      <button data-act="del" class="danger" ${S.models.length <= 1 ? 'disabled' : ''}>删除</button>
    </div>`;

  const bind = (act, handler) => {
    const btn = row.querySelector(`[data-act="${act}"]`);
    if (btn) btn.onclick = handler;
  };

  bind('toggle', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      await api(`/api/models/${model.id}/${state === 'ready' ? 'unload' : 'load'}`, { method: 'POST' });
      await loadModels();
      renderModelList();
    } catch (err) {
      toast(`失败：${err.message}`, 3000);
    } finally {
      btn.disabled = false;
    }
  });
  bind('use', async (e) => {
    e.currentTarget.disabled = true;
    try {
      await useModel(model.id);
      toast(`已切换到 ${model.label}`);
    } catch (err) {
      toast(`切换失败：${err.message}`, 3000);
    } finally {
      e.currentTarget.disabled = false;
    }
  });
  bind('edit', () => openModelEditor(model));
  bind('del', () => deleteModel(model));
  return row;
}

async function useModel(modelId) {
  if (S.conv) {
    S.conv = await api(`/api/conversations/${S.conv.id}`, { method: 'PATCH', body: { model_id: modelId } });
  }
  await saveConfig({ active_model_id: modelId });
  updateHeader();
  await loadModels();
  renderModelSelect();
  renderModelCard();
  renderModelList();
}

async function deleteModel(model) {
  if (!confirm(`删除模型「${model.label || model.id}」？`)) return;
  const models = S.config.models.filter((m) => m.id !== model.id);
  if (!models.length) {
    toast('至少要保留一个模型', 2600);
    return;
  }
  const active =
    S.config.active_model_id === model.id ? models[0].id : S.config.active_model_id;
  await saveConfig({ models, active_model_id: active });
  await loadModels();
  renderModelSelect();
  renderModelCard();
  renderModelList();
  toast('已删除');
}

async function deleteConnection(conn) {
  const owned = S.config.models.filter((m) => m.connection_id === conn.id);
  const tip = owned.length
    ? `删除连接「${conn.label}」会同时删掉它下面的 ${owned.length} 个模型，继续？`
    : `删除连接「${conn.label}」？`;
  if (!confirm(tip)) return;
  const models = S.config.models.filter((m) => m.connection_id !== conn.id);
  if (!models.length) {
    toast('至少要保留一个模型', 2600);
    return;
  }
  const connections = (S.config.connections || []).filter((c) => c.id !== conn.id);
  const active = models.some((m) => m.id === S.config.active_model_id)
    ? S.config.active_model_id
    : models[0].id;
  await saveConfig({ connections, models, active_model_id: active });
  await loadModels();
  renderModelSelect();
  renderModelCard();
  renderModelList();
  toast('已删除连接');
}

async function probeConnFromList(conn) {
  toast('正在获取模型列表…', 1600);
  try {
    const data = await api('/api/connections/probe', {
      method: 'POST',
      body: { connection_id: conn.id, base_url: conn.base_url },
    });
    const names = (data.models || []).map((m) => m.id);
    if (!data.ok) {
      toast(`获取失败：${data.detail}`, 3600);
      return;
    }
    const missing = names.filter((n) => !(S.config.models || []).some((m) => m.connection_id === conn.id && m.model === n));
    const hint = missing.length
      ? `服务端 ${names.length} 个模型，还有 ${missing.length} 个没添加：${missing.slice(0, 3).join('、')}${missing.length > 3 ? '…' : ''}`
      : `服务端 ${names.length} 个模型，都已添加`;
    toast(hint, 4200);
  } catch (err) {
    toast(`获取失败：${err.message}`, 3600);
  }
}

async function loadTools() {
  const data = await api('/api/tools');
  S.tools = data.tools || [];
}

function renderToolGrid() {
  const grid = $('#toolGrid');
  if (!grid) return;
  const disabled = new Set(S.config?.tools?.disabled || []);
  grid.innerHTML = '';
  S.tools.forEach((tool) => {
    const label = el('label', 'tool-item');
    label.innerHTML = `
      <input type="checkbox" ${disabled.has(tool.name) ? '' : 'checked'} data-tool="${tool.name}" />
      <div><b>${esc(tool.name)}</b><span>${esc(tool.description)}</span></div>`;
    grid.appendChild(label);
  });
}

function configModel(id) {
  return (S.config?.models || []).find((m) => m.id === id) || null;
}

/* ---------------------------- 模型库（下载） ---------------------------- */

function targetPathOf(repo) {
  const clean = String(repo || '').trim().replace(/^\/+|\/+$/g, '');
  if (clean.split('/').length !== 2) return '';
  return `models/${clean.replace('/', '--')}`;
}

function updateRepoHint() {
  const path = targetPathOf($('#mRepoId').value);
  $('#mRepoHint').textContent = path
    ? `下载到 ${path}，完成后自动加入列表`
    : '下载到 models/组织--模型名，完成后自动加入列表';
}

function setLocalMode(mode) {
  S.localMode = mode === 'path' ? 'path' : 'hub';
  $$('#mLocalMode button').forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === S.localMode)
  );
  $$('#modelModal [data-local]').forEach((n) => {
    n.hidden = n.dataset.local !== S.localMode;
  });
  syncSaveButton();
}

function syncSaveButton() {
  const isLocal = $('#mProvider').value === 'local_hf';
  $('#saveModel').hidden = isLocal && S.localMode === 'hub';
}

function syncHubSource() {
  $('#hubEndpointItem').hidden = $('#hubSource').value !== 'huggingface';
}

async function loadHub() {
  try {
    const data = await api('/api/hub');
    $('#hubSource').value = data.source || 'modelscope';
    $('#hubCache').value = data.cache_dir || 'models/.cache';
    $('#hubEndpoint').value = data.hf_endpoint || '';
    syncHubSource();
    $('#repoHints').innerHTML = (data.known || [])
      .map((r) => `<option value="${esc(r)}"></option>`)
      .join('');
    $('#hubHint').textContent = `下载路径固定为 models/组织--模型名；缓存落在 ${data.resolved_cache}`;
  } catch (_) {
    /* 忽略：模型库设置面板不可用时不影响其他功能 */
  }
}

async function saveHub() {
  await saveConfig({
    hub: {
      source: $('#hubSource').value,
      cache_dir: $('#hubCache').value.trim() || 'models/.cache',
      hf_endpoint: $('#hubEndpoint').value.trim(),
    },
  });
  await loadHub();
  toast('模型库设置已保存');
}

function fmtBytes(n) {
  const num = Number(n) || 0;
  if (num >= 1024 ** 3) return `${(num / 1024 ** 3).toFixed(2)} GB`;
  if (num >= 1024 ** 2) return `${(num / 1024 ** 2).toFixed(1)} MB`;
  if (num >= 1024) return `${(num / 1024).toFixed(0)} KB`;
  return `${num} B`;
}

function renderDownload(t) {
  $('#mDlBox').hidden = false;
  const total = Number(t.total) || 0;
  const bytes = Number(t.bytes) || 0;
  const pct = total > 0 ? Math.min(100, (bytes / total) * 100) : 0;
  const bar = $('#mDlBar');
  bar.classList.toggle('indeterminate', total <= 0);
  bar.style.width = total > 0 ? `${pct.toFixed(1)}%` : '100%';

  const parts = [];
  if (total > 0) parts.push(`${pct.toFixed(0)}%`);
  parts.push(total > 0 ? `${fmtBytes(bytes)} / ${fmtBytes(total)}` : fmtBytes(bytes));
  if (t.speed > 0) parts.push(`${fmtBytes(t.speed)}/s`);
  parts.push(t.detail || t.state);
  $('#mDlText').textContent = parts.join(' · ');
  $('#mDlCancel').hidden = t.state !== 'downloading';
}

async function startModelDownload() {
  const repo = $('#mRepoId').value.trim();
  if (repo.split('/').length !== 2 || !repo) {
    toast('模型 ID 需要形如 Qwen/Qwen3-0.6B', 3200);
    return;
  }
  const btn = $('#mDownload');
  btn.disabled = true;
  try {
    const task = await api('/api/models/download', {
      method: 'POST',
      body: { repo_id: repo, source: $('#hubSource').value },
    });
    S.dlTask = task.id;
    renderDownload({ ...task, state: 'downloading', detail: '正在连接…' });
    clearTimeout(S.dlTimer);
    S.dlTimer = setTimeout(pollDownload, 600);
  } catch (err) {
    toast(`启动下载失败：${err.message}`, 4000);
    btn.disabled = false;
  }
}

async function pollDownload() {
  clearTimeout(S.dlTimer);
  if (!S.dlTask) return;
  try {
    const t = await api(`/api/models/download/${S.dlTask}`);
    renderDownload(t);
    if (t.state === 'downloading') {
      S.dlTimer = setTimeout(pollDownload, 900);
      return;
    }
    $('#mDownload').disabled = false;
    if (t.state === 'done') {
      toast(`已下载并添加：${t.repo_id}`, 3200);
      await loadModels();
      renderModelSelect();
      renderModelCard();
      renderModelList();
      closeModal('modelModal');
    } else if (t.state === 'error') {
      toast(`下载失败：${t.error || t.detail}`, 6000);
    }
  } catch (_) {
    S.dlTimer = setTimeout(pollDownload, 1600);
  }
}

async function cancelDownload() {
  if (!S.dlTask) return;
  try {
    await api(`/api/models/download/${S.dlTask}/cancel`, { method: 'POST' });
  } catch (_) {
    /* 任务已结束时忽略 */
  }
  clearTimeout(S.dlTimer);
  $('#mDlBox').hidden = true;
  $('#mDownload').disabled = false;
  toast('已取消下载');
}

function fillConnSelect(selected) {
  const select = $('#mConn');
  const conns = S.config?.connections || [];
  if (!conns.length) {
    select.innerHTML = '<option value="">（还没有连接，先去「连接」里添加）</option>';
    return;
  }
  select.innerHTML = conns
    .map((c) => `<option value="${c.id}">${esc(c.label)} · ${esc(c.base_url || '未填地址')}</option>`)
    .join('');
  if (selected) select.value = selected;
}

/**
 * 打开模型编辑弹窗。
 * 关键：runtimeModel 来自 /api/models，只有运行状态、没有 base_url/model/context_window 等档案字段，
 * 所以这里必须回查 S.config.models 取完整配置，否则一保存就把原配置覆盖成默认值。
 */
function openModelEditor(runtimeModel, provider = null, connId = null) {
  const existing = runtimeModel ? configModel(runtimeModel.id) : null;
  const model = existing || null;
  S.editingModelId = model?.id || null;

  $('#modelModalTitle').textContent = model ? '编辑模型' : '添加模型';
  const prov = provider || model?.provider || 'openai_compat';
  $('#mProvider').value = prov;
  $('#mLabel').value = model?.provider === 'local_hf' ? model.label || '' : '';
  $('#mPath').value = model?.model_path || 'models/Qwen/Qwen3-0___6B';
  $('#mDevice').value = model?.device || 'auto';
  $('#mDtype').value = model?.dtype || 'auto';
  $('#mThinking').checked = !!model?.enable_thinking;
  $('#mRemoteLabel').value = model?.provider === 'openai_compat' ? model.label || '' : '';
  $('#mContext').value = model?.context_window || 32768;
  $('#mThinkParam').value = model?.think_param || 'auto';
  $('#mVision').value = model?.vision || 'auto';
  $('#mRemoteLabel').value = model?.provider === 'openai_compat' ? model.label || '' : '';
  $('#mManualName').value = '';
  $('#mRepoId').value = '';
  $('#mDlBox').hidden = true;
  $('#mDownload').disabled = false;
  updateRepoHint();
  setLocalMode(model?.model_path ? 'path' : 'hub');
  const activeConn = connId || model?.connection_id || '';
  fillConnSelect(activeConn);
  resetModelPicker(activeConn, model);
  toggleProviderFields();
  openModal('modelModal');
}

/** 模型弹窗里的多选：owned = 该连接下已建档的模型名，编辑时只能对应一个 */
function resetModelPicker(connId, editingModel) {
  const owned = (S.config.models || [])
    .filter((m) => m.provider !== 'local_hf' && m.connection_id === connId)
    .map((m) => m.model)
    .filter(Boolean);
  const current =
    editingModel && editingModel.provider === 'openai_compat' ? editingModel.model : null;
  pickerReset('model', owned.filter((n) => n !== current), current ? [current] : [], {
    single: !!editingModel,
  });
  $('#mFetchHint').textContent = editingModel
    ? '编辑时只能对应一个模型；要批量添加请新建。'
    : '从连接拉取模型列表，搜索勾选，可一次添加多个。';
}

function toggleProviderFields() {
  const provider = $('#mProvider').value;
  $$('[data-provider]').forEach((node) => {
    node.hidden = node.dataset.provider !== provider;
  });
  syncSaveButton();
}

async function fetchModelsForModelForm() {
  const btn = $('#mFetchModels');
  const connId = $('#mConn').value;
  if (!connId) {
    $('#mFetchHint').textContent = '先选一个连接';
    return;
  }
  btn.disabled = true;
  $('#mFetchHint').textContent = '正在请求…';
  try {
    const data = await api('/api/connections/probe', {
      method: 'POST',
      body: { connection_id: connId },
    });
    if (!data.ok) {
      $('#mFetchHint').textContent = data.detail || '获取失败';
      return;
    }
    const names = (data.models || []).map((m) => m.id);
    pickerAdd('model', names);
    openPickerPanel('model');
    $('#mFetchHint').textContent = names.length
      ? `${names.length} 个模型可用，搜索勾选（可多选）`
      : '服务端没返回列表，可以在下面手动填模型名';
  } catch (err) {
    $('#mFetchHint').textContent = `获取失败：${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

/** 手填模型名：加入候选并直接勾选 */
function addManualModelName() {
  const name = $('#mManualName').value.trim();
  if (!name) return;
  pickerAdd('model', [name]);
  if (!PICKERS.model.selected.has(name)) pickerToggle('model', name);
  $('#mManualName').value = '';
}

async function saveModel() {
  const provider = $('#mProvider').value;
  if (provider === 'local_hf' && S.localMode === 'hub') {
    // 下载模式下，保存按钮就是「开始下载」
    startModelDownload();
    return;
  }
  const id = S.editingModelId || `m_${Date.now().toString(36)}`;
  const existing = S.editingModelId ? configModel(S.editingModelId) : null;
  let model;

  if (provider === 'local_hf') {
    model = {
      ...(existing || {}),
      id,
      provider,
      label: $('#mLabel').value.trim() || '本地模型',
      model_path: $('#mPath').value.trim(),
      device: $('#mDevice').value,
      dtype: $('#mDtype').value,
      enable_thinking: $('#mThinking').checked,
      load_on_start: existing?.load_on_start ?? false,
      vision: $('#mVision').value,
      context_window: Number($('#mContext').value) || 32768,
    };
  } else {
    const conn = (S.config.connections || []).find((c) => c.id === $('#mConn').value);
    if (!conn) {
      toast('请先添加并选择一个连接', 3200);
      return;
    }
    const picked = [...PICKERS.model.selected];
    if (!picked.length) {
      toast('请先勾选至少一个模型', 2600);
      return;
    }
    const shared = {
      provider,
      connection_id: conn.id,
      base_url: conn.base_url,
      vision: $('#mVision').value,
      think_param: $('#mThinkParam').value,
      context_window: Number($('#mContext').value) || 32768,
    };
    const customLabel = $('#mRemoteLabel').value.trim();

    // 编辑：一个档案只对应一个模型
    if (existing) {
      const name = picked[0];
      const clash = (S.config.models || []).find(
        (m) => m.id !== id && m.connection_id === conn.id && m.model === name
      );
      if (clash) {
        toast(`「${name}」已经添加过了`, 2800);
        return;
      }
      model = { ...existing, ...shared, id, model: name, label: customLabel || name };
      const models = [...S.config.models];
      const idx = models.findIndex((m) => m.id === id);
      if (idx >= 0) models[idx] = { ...models[idx], ...model };
      else models.push(model);
      await saveConfig({ models });
      closeModal('modelModal');
      await loadModels();
      renderModelSelect();
      renderModelCard();
      renderModelList();
      toast('已保存');
      return;
    }

    // 新增：一次可建多个档案，已存在的跳过
    const taken = new Set(
      (S.config.models || [])
        .filter((m) => m.connection_id === conn.id)
        .map((m) => m.model)
    );
    const fresh = picked.filter((n) => !taken.has(n));
    if (!fresh.length) {
      toast('勾选的模型都已经添加过了', 2800);
      return;
    }
    const stamp = Date.now().toString(36);
    const models = [...S.config.models];
    fresh.forEach((name, i) => {
      models.push({
        ...shared,
        id: `m_${stamp}${i}`,
        label: fresh.length === 1 && customLabel ? customLabel : name,
        model: name,
      });
    });
    await saveConfig({ models });
    closeModal('modelModal');
    await loadModels();
    renderModelSelect();
    renderModelCard();
    renderModelList();
    const skipped = picked.length - fresh.length;
    toast(`已添加 ${fresh.length} 个模型${skipped ? `，跳过 ${skipped} 个已存在` : ''}`);
    return;
  }

  const models = [...S.config.models];
  const idx = models.findIndex((m) => m.id === id);
  if (idx >= 0) models[idx] = { ...models[idx], ...model };
  else models.push(model);
  await saveConfig({ models });
  closeModal('modelModal');
  await loadModels();
  renderModelSelect();
  renderModelCard();
  renderModelList();
  toast('已保存');
}

/* ---------------------------- 可搜索多选下拉 ----------------------------
 * 连接弹窗（ms*）与模型弹窗（mm*）共用同一套实现，按实例 key 区分：
 *   all      可选项全集
 *   owned    该连接下已存在的模型名（列表里标「已添加」）
 *   selected 当前勾选
 *   single   true = 单选（编辑已有模型时只能对应一个）
 */
const PICKERS = {
  conn: { key: 'ms', all: [], owned: new Set(), selected: new Set(), single: false },
  model: { key: 'mm', all: [], owned: new Set(), selected: new Set(), single: false },
};

function pickerEls(which) {
  const p = PICKERS[which].key;
  return {
    wrap: $(`#${p}Wrap`),
    control: $(`#${p}Control`),
    tags: $(`#${p}Tags`),
    search: $(`#${p}Search`),
    panel: $(`#${p}Panel`),
    options: $(`#${p}Options`),
    summary: $(`#${p}Summary`),
  };
}

function pickerReset(which, ownedNames, selectedNames = null, opts = {}) {
  const st = PICKERS[which];
  st.owned = new Set(ownedNames || []);
  st.selected = new Set(selectedNames || []);
  st.all = [...new Set([...st.owned, ...st.selected, ...(opts.extra || [])])];
  st.single = !!opts.single;
  const els = pickerEls(which);
  if (els.search) els.search.value = '';
  renderPicker(which);
}

function pickerAdd(which, names) {
  const st = PICKERS[which];
  st.all = [...new Set([...st.all, ...(names || [])])];
  renderPicker(which);
}

function pickerToggle(which, name) {
  const st = PICKERS[which];
  if (st.selected.has(name)) {
    st.selected.delete(name);
  } else {
    if (st.single) st.selected.clear();
    st.selected.add(name);
  }
  renderPicker(which);
}

function pickerFiltered(which) {
  const st = PICKERS[which];
  const els = pickerEls(which);
  const q = (els.search?.value || '').trim().toLowerCase();
  return st.all.filter((n) => !q || n.toLowerCase().includes(q));
}

function renderPicker(which) {
  const st = PICKERS[which];
  const els = pickerEls(which);
  if (!els.tags || !els.options) return;

  els.tags.innerHTML = [...st.selected]
    .map(
      (n) => `<span class="ms-tag" data-tag="${esc(n)}">
        <span>${esc(n)}</span>
        <button type="button" data-drop="${esc(n)}" title="移除">✕</button>
      </span>`
    )
    .join('');
  els.search.placeholder = st.selected.size ? '' : '搜索模型…';

  const q = (els.search.value || '').trim().toLowerCase();
  const list = pickerFiltered(which);

  if (!st.all.length) {
    els.options.innerHTML = `<p class="ms-empty">${
      which === 'model' ? '还没有可选模型，点「获取并选择」或手填' : '还没有模型，先点「获取模型列表」'
    }</p>`;
    els.summary.textContent = '暂无模型';
    return;
  }
  if (!list.length) {
    els.options.innerHTML = `<p class="ms-empty">没有匹配「${esc(q)}」的模型</p>`;
  } else {
    els.options.innerHTML = list
      .map((n) => {
        const on = st.selected.has(n);
        const owned = st.owned.has(n) && !on;
        return `<div class="ms-opt ${on ? 'on' : ''}" data-model="${esc(n)}">
          <span class="ms-check">${on ? '✓' : ''}</span>
          <span class="ms-name">${esc(n)}</span>
          ${owned ? '<span class="tag gray">已添加</span>' : ''}
        </div>`;
      })
      .join('');
  }
  const tail = st.single ? '（单选）' : '';
  els.summary.textContent = `已选 ${st.selected.size} / 共 ${st.all.length} 个${tail}`;
}

function positionPickerPanel(which) {
  const els = pickerEls(which);
  const ctrl = els.control;
  const panel = els.panel;
  if (!ctrl || !panel) return;
  const r = ctrl.getBoundingClientRect();
  const below = window.innerHeight - r.bottom - 12;
  const above = r.top - 12;
  panel.style.left = `${r.left}px`;
  panel.style.width = `${r.width}px`;
  if (below >= 180 || below >= above) {
    panel.style.top = `${r.bottom + 4}px`;
    panel.style.bottom = 'auto';
    panel.style.maxHeight = `${Math.max(120, Math.min(300, below))}px`;
  } else {
    panel.style.top = 'auto';
    panel.style.bottom = `${window.innerHeight - r.top + 4}px`;
    panel.style.maxHeight = `${Math.max(120, Math.min(300, above))}px`;
  }
}

function openPickerPanel(which) {
  const els = pickerEls(which);
  if (!els.panel || !els.panel.hidden) return;
  // 同时只开一个面板，避免两个弹窗的面板互相遮挡
  Object.keys(PICKERS).forEach((k) => {
    if (k !== which) closePickerPanel(k);
  });
  els.panel.hidden = false;
  positionPickerPanel(which);
  renderPicker(which);
  els.search.focus();
}

function closePickerPanel(which) {
  const els = pickerEls(which);
  if (els.panel) els.panel.hidden = true;
}

function bindPicker(which) {
  const els = pickerEls(which);
  if (!els.control) return;
  els.control.onclick = () => openPickerPanel(which);
  els.search.oninput = () => renderPicker(which);
  els.search.onkeydown = (e) => {
    // Esc 只关下拉，不要冒泡到全局快捷键把整个弹窗关掉
    e.stopPropagation();
    if (e.key === 'Escape') {
      closePickerPanel(which);
      els.search.blur();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const first = els.options.querySelector('.ms-opt');
      if (first) {
        pickerToggle(which, first.dataset.model);
        els.search.value = '';
        renderPicker(which);
      }
    } else if (e.key === 'Backspace' && !els.search.value) {
      const last = [...PICKERS[which].selected].pop();
      if (last) pickerToggle(which, last);
    }
  };
  els.tags.onclick = (e) => {
    const btn = e.target.closest('[data-drop]');
    if (!btn) return;
    e.stopPropagation();
    pickerToggle(which, btn.dataset.drop);
  };
  els.options.onclick = (e) => {
    const opt = e.target.closest('.ms-opt');
    if (opt) pickerToggle(which, opt.dataset.model);
  };
  els.panel.querySelectorAll('[data-ms]').forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const visible = pickerFiltered(which);
      if (btn.dataset.ms === 'all') {
        if (PICKERS[which].single) {
          PICKERS[which].selected = new Set(visible.slice(0, 1));
        } else {
          visible.forEach((n) => PICKERS[which].selected.add(n));
        }
      } else {
        visible.forEach((n) => PICKERS[which].selected.delete(n));
      }
      renderPicker(which);
    };
  });
}

/* ---------------------------- 连接编辑 ---------------------------- */

function openConnEditor(conn) {
  S.editingConnId = conn?.id || null;
  $('#connModalTitle').textContent = conn ? '编辑连接' : '添加连接';
  $('#cLabel').value = conn?.label || '';
  $('#cBaseUrl').value = conn?.base_url || '';
  $('#cApiKey').value = '';
  $('#cApiKey').placeholder = conn?.has_api_key
    ? '已保存密钥，留空即不修改'
    : '粘贴 API Key（本地服务可留空）';
  $('#cKeyHint').textContent = conn?.has_api_key
    ? '已保存密钥，留空即沿用；要换就直接填新的。'
    : '留空表示不带密钥（LM Studio / Ollama 一般不需要）。';
  $('#cFetchHint').textContent = '先从服务端拉取列表，再搜索勾选。';
  const owned = conn
    ? (S.config.models || [])
        .filter((m) => m.connection_id === conn.id)
        .map((m) => m.model)
    : [];
  pickerReset('conn', owned, owned);
  closePickerPanel('conn');
  openModal('connModal');
}

async function fetchConnModels() {
  const btn = $('#cFetchModels');
  btn.disabled = true;
  $('#cFetchHint').textContent = '正在请求…';
  try {
    const data = await api('/api/connections/probe', {
      method: 'POST',
      body: {
        connection_id: S.editingConnId || '',
        base_url: $('#cBaseUrl').value.trim(),
        api_key: $('#cApiKey').value,
      },
    });
    if (!data.ok) {
      $('#cFetchHint').textContent = data.detail || '获取失败';
      return;
    }
    const names = (data.models || []).map((m) => m.id);
    pickerAdd('conn', names);
    openPickerPanel('conn');
    $('#cFetchHint').textContent = names.length
      ? `${names.length} 个模型可用，搜索并勾选要用的`
      : '服务端没有返回模型，可以在「添加模型」里手填';
  } catch (err) {
    $('#cFetchHint').textContent = `获取失败：${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function saveConn() {
  const id = S.editingConnId || `c${Date.now().toString(36)}`;
  const base_url = $('#cBaseUrl').value.trim();
  if (!base_url) {
    toast('请填写服务地址', 2600);
    return;
  }
  const conn = {
    id,
    label: $('#cLabel').value.trim() || base_url,
    base_url,
    api_key: $('#cApiKey').value,
  };
  const connections = [...(S.config.connections || [])];
  const idx = connections.findIndex((c) => c.id === id);
  if (idx >= 0) connections[idx] = { ...connections[idx], ...conn };
  else connections.push(conn);

  const picked = [...PICKERS.conn.selected];

  let models = S.config.models.filter((m) => m.connection_id !== id || picked.includes(m.model));
  models = models.map((m) => (m.connection_id === id ? { ...m, base_url } : m));

  const existingNames = new Set(
    models.filter((m) => m.connection_id === id).map((m) => m.model)
  );
  const defaultCtx =
    S.config.models.find((m) => m.connection_id === id)?.context_window || 32768;
  picked
    .filter((n) => !existingNames.has(n))
    .forEach((n, i) => {
      models.push({
        id: `m_${Date.now().toString(36)}${i}`,
        label: n,
        provider: 'openai_compat',
        connection_id: id,
        model: n,
        base_url,
        vision: 'auto',
        context_window: defaultCtx,
      });
    });

  if (!models.length) {
    toast('至少要保留一个模型', 2800);
    return;
  }
  const active = models.some((m) => m.id === S.config.active_model_id)
    ? S.config.active_model_id
    : models[0].id;

  await saveConfig({ connections, models, active_model_id: active });
  closeModal('connModal');
  await loadModels();
  renderModelSelect();
  renderModelCard();
  renderModelList();
  toast('已保存');
}

async function loadSystemInfo() {
  try {
    const info = await api('/api/system');
    const rows = [
      ['Python', info.python],
      ['系统', info.platform],
      ['推理设备', info.device === 'cuda' ? `CUDA · ${info.device_name}（${info.total_vram} GB）` : `${info.device_name || 'CPU'} · ${info.cpu_count || ''} 核`],
      ['torch', info.torch || '未安装'],
      ['transformers', info.transformers || '未安装'],
    ];
    $('#systemInfo').innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v ?? '-')}</dd>`).join('');
  } catch (_) {}
}

async function saveConfig(patch) {
  const cfg = await api('/api/config', { method: 'PUT', body: patch });
  S.config = cfg;
  return cfg;
}

async function savePreferences() {
  const disabled = $$('#toolGrid input[data-tool]')
    .filter((i) => !i.checked)
    .map((i) => i.dataset.tool);
  const cfg = await saveConfig({
    defaults: {
      system_prompt: $('#prefSystem').value,
      temperature: Number($('#prefTemp').value),
      top_p: Number($('#prefTopP').value),
      max_tokens: Number($('#prefMaxTokens').value),
      auto_compact: $('#prefAutoCompact').checked,
      compact_mode: $('#prefCompactMode').value,
      compact_tokens: Number($('#prefCompactTokens').value),
      compact_on_overflow: $('#prefCompactOnOverflow').checked,
      compact_threshold: Number($('#prefThreshold').value),
      compact_keep_recent: Number($('#prefKeepRecent').value),
    },
    tools: {
      enabled: $('#prefTools').checked,
      disabled,
      max_rounds: Number($('#prefMaxRounds').value),
      allow_shell: $('#prefShell').checked,
    },
  });
  // 后端会把轮数夹到 1~limit 之间，回填一下让输入框显示真实生效值
  $('#prefMaxRounds').value = cfg.tools.max_rounds;
  $('#prefCompactTokens').value = cfg.defaults.compact_tokens;
  await loadModels();
  updateHeader();
  toast('偏好已保存');
}

/* ---------------------------- 性能面板 ---------------------------- */

function fillPerfForm(perf) {
  perf = perf || {};
  $('#perfCache').checked = perf.prefix_cache !== false;
  $('#perfCacheTokens').value = String(perf.cache_tokens || 4096);
  $('#perfCacheEntries').value = perf.cache_entries || 2;
  $('#perfWarm').checked = perf.warm_prefix !== false;
  $('#perfInt8').checked = !!perf.int8;
  $('#perfThreads').value = perf.threads || 0;
}

async function loadPerf() {
  try {
    const info = await api('/api/perf');
    fillPerfForm(info.config);
    const c = info.cache || {};
    const p = info.config || {};
    const total = (c.hits || 0) + (c.misses || 0);
    const rows = [
      ['缓存状态', p.prefix_cache === false ? '已关闭' : `开启 · ${c.entries || 0} 条`],
      ['命中率', total ? `${((c.hit_rate || 0) * 100).toFixed(0)}%（${c.hits} 命中 / ${c.misses} 未命中）` : '还没有请求'],
      ['已缓存', `${(c.cached_tokens || 0).toLocaleString()} token · 约 ${p.cached_mb || 0} MB`],
      ['累计省下', `${(c.reused_tokens || 0).toLocaleString()} token 的重复读入`],
    ];
    $('#perfStats').innerHTML = rows
      .map(([k, v]) => `${esc(k)}：<b>${esc(String(v))}</b>`)
      .join('<br/>');
  } catch (_) {
    /* 非本地后端时忽略 */
  }
}

async function savePerf() {
  const before = S.config.perf || {};
  const perf = {
    prefix_cache: $('#perfCache').checked,
    cache_tokens: Number($('#perfCacheTokens').value),
    cache_entries: Number($('#perfCacheEntries').value),
    warm_prefix: $('#perfWarm').checked,
    int8: $('#perfInt8').checked,
    threads: Number($('#perfThreads').value) || 0,
  };
  const needsReload =
    !!before.int8 !== perf.int8 || (before.threads || 0) !== perf.threads;
  try {
    await saveConfig({ perf });
    if (needsReload) {
      const model = activeModel();
      if (model?.provider === 'local_hf') {
        toast('正在按新参数重载模型…', 4000);
        await api(`/api/models/${encodeURIComponent(model.id)}/load`, { method: 'POST' });
      }
    }
    await loadPerf();
    await loadModels();
    updateHeader();
    toast(needsReload ? '已保存并重载模型' : '性能设置已保存');
  } catch (err) {
    toast(`保存失败：${err.message}`, 3200);
  }
}

/* ---------------------------- Token 用量面板 ---------------------------- */

const USAGE_IN_COLORS = {
  fixed: '#5b6bf0',
  history: '#3f9bf5',
  question: '#efa13c',
  tool_results: '#34b884',
};
const USAGE_IN_LABELS = {
  fixed: '系统提示 + 工具 schema',
  history: '历史消息',
  question: '本轮问题',
  tool_results: '工具返回',
};
const USAGE_OUT_COLORS = {
  reasoning: '#a78bfa',
  content: '#4f9cf9',
  tool_calls: '#33b58b',
  other: '#9aa3b2',
};
const USAGE_OUT_LABELS = {
  reasoning: '思考',
  content: '正文',
  tool_calls: '工具调用',
  other: '模板特殊符号',
};

function fmtTok(value) {
  return Number(value || 0).toLocaleString('en-US');
}

function fmtSec(value) {
  const n = Number(value || 0);
  if (!n) return '0s';
  return n >= 60 ? `${Math.floor(n / 60)}m${Math.round(n % 60)}s` : `${n.toFixed(1)}s`;
}

function openUsage() {
  if (!S.activeId) {
    toast('先开始一段对话');
    return;
  }
  closeAllPopovers();
  openPage('usagePage', 'usage');
  loadUsage();
}

function closeUsage() {
  closePage();
}

async function loadUsage() {
  if (!S.activeId) return;
  const body = $('#usageBody');
  try {
    const data = await api(`/api/conversations/${S.activeId}/usage`);
    S.usage = data;
    renderUsage(data);
  } catch (err) {
    body.querySelectorAll('.usage-block, .usage-cards').forEach((n) => (n.hidden = true));
    let empty = body.querySelector('.usage-empty');
    if (!empty) {
      empty = el('div', 'usage-empty');
      body.appendChild(empty);
    }
    empty.hidden = false;
    empty.textContent = `读取用量失败：${err.message}`;
  }
}

function usageBar(bar, legend, items, total) {
  bar.innerHTML = '';
  legend.innerHTML = '';
  if (!total) {
    bar.appendChild(el('i')).style.cssText = 'width:100%;background:var(--border)';
  }
  items.forEach((item) => {
    const pct = total ? (item.value / total) * 100 : 0;
    if (item.value > 0) {
      const seg = el('i');
      seg.style.width = `${pct}%`;
      seg.style.background = item.color;
      seg.title = `${item.label} ${fmtTok(item.value)} tok（${pct.toFixed(1)}%）`;
      bar.appendChild(seg);
    }
    const lg = el('div', 'lg');
    lg.innerHTML =
      `<span class="dot" style="background:${item.color}"></span>` +
      `<span>${esc(item.label)}</span><b>${fmtTok(item.value)}</b>` +
      (total ? `<span class="sub">${pct.toFixed(1)}%</span>` : '');
    legend.appendChild(lg);
  });
}

function usageCard(label, value, sub) {
  const card = el('div', 'usage-card');
  card.innerHTML =
    `<div class="k">${esc(label)}</div><div class="v">${esc(value)}</div>` +
    `<div class="s">${sub}</div>`;
  return card;
}

function renderUsage(data) {
  const totals = data.totals || {};
  const input = totals.input || {};
  const output = totals.output || {};
  const tool = totals.tool || {};
  const ctx = totals.context || {};

  $('#usageSub').textContent =
    `${data.model || '本地模型'} · ${totals.turns || 0} 轮 / ${totals.models || 0} 次生成` +
    (tool.calls ? ` / ${tool.calls} 次工具` : '');
  $('#usageBody').querySelectorAll('.usage-block, .usage-cards').forEach((n) => (n.hidden = false));
  const empty = $('#usageBody').querySelector('.usage-empty');
  if (empty) empty.hidden = true;

  // ---- 概览卡片 ----
  const cards = $('#usageCards');
  cards.innerHTML = '';
  cards.appendChild(
    usageCard(
      '输入合计',
      `${fmtTok(input.prompt)} tok`,
      `缓存复用 ${fmtTok(input.reused)}（${input.cache_rate || 0}%）<br/>实算 ${fmtTok(input.prefill)}`
    )
  );
  cards.appendChild(
    usageCard(
      '输出合计',
      `${fmtTok(output.total)} tok`,
      `思考 ${fmtTok(output.reasoning)} · 正文 ${fmtTok(output.content)}<br/>` +
        `工具调用 ${fmtTok(output.tool_calls)}` +
        (output.other ? ` · 其他 ${fmtTok(output.other)}` : '')
    )
  );
  cards.appendChild(
    usageCard(
      '工具调用',
      `${fmtTok(tool.calls)} 次`,
      tool.calls
        ? `失败 ${fmtTok(tool.failures)} · 返回 ${fmtTok(tool.result_tokens)} tok<br/>耗时 ${fmtSec(tool.duration)}`
        : '本会话还没有调用工具'
    )
  );
  cards.appendChild(
    usageCard(
      '当前上下文',
      `${fmtTok(ctx.tokens)} tok`,
      `上限 ${fmtTok(ctx.limit)}（${ctx.percent || 0}%）<br/>提问合计 ${fmtTok(totals.question_tokens)} tok`
    )
  );

  // ---- 输入 / 输出构成 ----
  $('#usageInTotal').textContent = `合计 ${fmtTok(input.prompt)} tok`;
  usageBar(
    $('#usageInBar'),
    $('#usageInLegend'),
    ['fixed', 'history', 'question', 'tool_results'].map((key) => ({
      label: USAGE_IN_LABELS[key],
      value: (input.split || {})[key] || 0,
      color: USAGE_IN_COLORS[key],
    })),
    input.prompt
  );
  $('#usageOutTotal').textContent = `合计 ${fmtTok(output.total)} tok`;
  usageBar(
    $('#usageOutBar'),
    $('#usageOutLegend'),
    ['reasoning', 'content', 'tool_calls', 'other'].map((key) => ({
      label: USAGE_OUT_LABELS[key],
      value: output[key] || 0,
      color: USAGE_OUT_COLORS[key],
    })),
    output.total
  );

  // ---- 逐轮明细 ----
  const table = $('#usageTurns');
  table.innerHTML = '';
  const head = el('div', 'usage-row head');
  head.innerHTML =
    '<div>轮次</div><div class="num hide-sm">问题 tok</div><div>输入 tok</div>' +
    '<div>输出 tok</div><div>工具</div><div class="num hide-sm">耗时</div>';
  table.appendChild(head);

  (data.turns || []).forEach((turn) => {
    const row = el('div', 'usage-row body');
    const inp = turn.input || {};
    const out = turn.output || {};
    const tools = turn.tool_summary || {};
    const reuseRate = inp.prompt ? Math.round((inp.reused / inp.prompt) * 100) : 0;
    row.innerHTML =
      `<div class="num">#${turn.index}</div>` +
      `<div class="num hide-sm">${fmtTok(turn.question?.tokens)}</div>` +
      `<div class="cell"><div>${fmtTok(inp.prompt)}</div>` +
      `<div class="sub">复用 ${fmtTok(inp.reused)} · 实算 ${fmtTok(inp.prefill)}</div></div>` +
      `<div class="cell"><div>${fmtTok(out.total)}</div>` +
      `<div class="sub">思 ${fmtTok(out.reasoning)} · 文 ${fmtTok(out.content)} · 调 ${fmtTok(out.tool_calls)}</div></div>` +
      `<div class="cell"><div>${fmtTok(tools.calls)} 次</div>` +
      `<div class="sub">${fmtTok(tools.result_tokens)} tok</div></div>` +
      `<div class="num hide-sm">${fmtSec(turn.duration)}</div>`;
    row.title = turn.question?.preview ? `问题：${turn.question.preview}` : '';

    const detail = el('div', 'usage-detail');
    detail.hidden = true;
    let genIndex = 0;
    (turn.steps || []).forEach((step) => {
      const line = el('div', 'usage-step');
      if (step.type === 'model') {
        genIndex += 1;
        const split = step.input?.split || {};
        line.innerHTML =
          `<b>生成 #${genIndex}</b>` +
          (step.thinking ? '<span class="usage-badge">思考模式</span>' : '') +
          (step.input?.estimated ? '<span class="usage-badge">输入为估算</span>' : '') +
          `<span>输入 <b>${fmtTok(step.input?.prompt)}</b>（复用 ${fmtTok(step.input?.reused)}` +
          ` · 实算 ${fmtTok(step.input?.prefill)}）</span>` +
          `<span class="sub">固定 ${fmtTok(split.fixed)} · 历史 ${fmtTok(split.history)}` +
          ` · 问题 ${fmtTok(split.question)} · 工具 ${fmtTok(split.tool_results)}</span>` +
          `<span>输出 <b>${fmtTok(step.output?.total)}</b>（思考 ${fmtTok(step.output?.reasoning)}` +
          ` · 正文 ${fmtTok(step.output?.content)} · 调用 ${fmtTok(step.output?.tool_calls)}）</span>` +
          `<span class="sub">首字 ${step.ttft ? `${Number(step.ttft).toFixed(2)}s` : '—'}` +
          ` · ${step.tps ? `${step.tps} tok/s` : '—'} · ${fmtSec(step.duration)}</span>`;
      } else {
        line.innerHTML =
          `<b>工具 ${esc(step.name || '?')}</b>` +
          `<span class="usage-badge ${step.ok ? 'ok' : 'err'}">${step.ok ? '成功' : '失败'}</span>` +
          `<span>返回 <b>${fmtTok(step.result_tokens)}</b> tok</span>` +
          (step.args_tokens ? `<span class="sub">参数 ${fmtTok(step.args_tokens)} tok</span>` : '') +
          `<span class="sub">耗时 ${fmtSec(step.duration)}</span>` +
          (step.preview
            ? `<span class="sub usage-preview" title="${esc(step.preview)}">↳ ${esc(step.preview)}</span>`
            : '');
      }
      detail.appendChild(line);
    });
    if (!detail.children.length) {
      detail.appendChild(el('div', 'usage-step', '<span class="sub">该轮没有记录到生成步骤</span>'));
    }

    row.onclick = () => {
      detail.hidden = !detail.hidden;
      row.classList.toggle('open', !detail.hidden);
    };
    table.appendChild(row);
    table.appendChild(detail);
  });
  if (!(data.turns || []).length) {
    table.appendChild(el('div', 'usage-empty', '这个会话还没有产生 token 消耗'));
  }

  // ---- 工具排行 ----
  const toolsBox = $('#usageTools');
  toolsBox.innerHTML = '';
  const byTool = tool.by_tool || [];
  $('#usageToolSum').textContent = byTool.length
    ? `${tool.calls} 次调用 · 返回 ${fmtTok(tool.result_tokens)} tok · 失败 ${tool.failures}`
    : '无';
  if (!byTool.length) {
    $('#usageToolsBlock').hidden = true;
    return;
  }
  $('#usageToolsBlock').hidden = false;
  const thead = el('div', 'usage-row head');
  thead.style.gridTemplateColumns = 'minmax(0, 1fr) 62px 62px 92px';
  thead.innerHTML = '<div>工具</div><div class="num">次数</div><div class="num">失败</div><div class="num">返回 tok</div>';
  toolsBox.appendChild(thead);
  byTool.forEach((item) => {
    const row = el('div', 'usage-row');
    row.style.gridTemplateColumns = 'minmax(0, 1fr) 62px 62px 92px';
    row.innerHTML =
      `<div class="cell">${esc(item.name)}</div>` +
      `<div class="num">${fmtTok(item.calls)}</div>` +
      `<div class="num">${fmtTok(item.failures)}</div>` +
      `<div class="num">${fmtTok(item.tokens)}</div>`;
    toolsBox.appendChild(row);
  });
}

/* ---------------------------- 会话参数 ---------------------------- */

function openParams() {
  if (!S.conv) {
    toast('先创建会话再调整参数');
    return;
  }
  const conv = S.conv;
  $('#convSystem').value = conv.system_prompt || '';
  $('#convTemp').value = conv.temperature ?? 0.6;
  $('#convTempVal').textContent = Number(conv.temperature ?? 0.6).toFixed(2);
  $('#convTopP').value = conv.top_p ?? 0.95;
  $('#convTopPVal').textContent = Number(conv.top_p ?? 0.95).toFixed(2);
  $('#convMaxTokens').value = conv.max_tokens || 2048;
  const usage = S.ctx || {};
  $('#convContext').innerHTML = `已用约 <b>${usage.tokens || 0}</b> / ${usage.limit || 0} tokens（${usage.percent || 0}%）<br/>
    自动压缩阈值 ${Math.round((usage.threshold || 0.75) * 100)}%，触发后会把较早消息总结成摘要。`;
  refreshContext();
  openModal('paramsModal');
}

async function saveParams() {
  if (!S.conv) return;
  const conv = await api(`/api/conversations/${S.conv.id}`, {
    method: 'PATCH',
    body: {
      system_prompt: $('#convSystem').value,
      temperature: Number($('#convTemp').value),
      top_p: Number($('#convTopP').value),
      max_tokens: Number($('#convMaxTokens').value),
    },
  });
  S.conv = conv;
  updateHeader();
  closeModal('paramsModal');
  toast('参数已更新');
}

/* ---------------------------- 输入框 ---------------------------- */

function autoGrow(node) {
  node.style.height = 'auto';
  node.style.height = Math.min(node.scrollHeight, 200) + 'px';
}

/* ---------------------------- 启动 ---------------------------- */

async function boot() {
  S.expanded = readExpanded();
  applyTheme(S.theme);
  S.config = await api('/api/config');
  S.pendingThink = {
    thinking: !!(S.config?.defaults?.thinking),
    level: S.config?.defaults?.thinking_level || 'medium',
  };
  await Promise.all([loadModels(), loadSkills()]);
  await syncRuns();
  await loadSpaces();
  await loadConversations();
  await loadSchedules().catch(() => {});
  updateHeader();
  const scopeSpaceId = S.scope.type === 'space' ? S.scope.id : null;
  const inScope = S.conversations.find((c) => (c.space_id || null) === scopeSpaceId);
  if (inScope) await openConversation(inScope.id);
  else {
    renderSidebar();
    await renderChat();
    updateHeader();
    syncRunUi();
  }
  if (localStorage.getItem('lm-ws-open') === '1') toggleWorkspace(true);

  // 运行状态：别的会话在生成时，侧边栏要能看见进度 / 徽标
  setInterval(syncRuns, 2000);

  // 定时刷新模型状态
  setInterval(async () => {
    if (document.hidden || Object.keys(S.runs).length) return;
    try {
      await loadModels();
    } catch (_) {}
  }, 6000);
}

/* ---------------------------- 事件绑定 ---------------------------- */

function bind() {
  $('#newChat').onclick = async () => {
    await createConversation();
    $('#input').focus();
  };

  // ---- 任务 / 空间 ----
  $('#newSpace').onclick = () => openSpaceModal();
  $('#scopeChip').onclick = () => openScopeMenu($('#scopeChip'));
  $('#spaceSubmit').onclick = submitSpace;
  $('#spaceName').oninput = renderSpacePreview;
  $('#spaceParent').oninput = renderSpacePreview;
  $('#spacePath').oninput = renderSpacePreview;
  $('#spaceMode').onclick = (e) => {
    const btn = e.target.closest('button[data-mode]');
    if (!btn) return;
    spaceState.mode = btn.dataset.mode;
    if (!spaceState.editing && spaceState.icon) {
      // 新建文件夹 / 关联目录 用不同的默认图标
      if (spaceState.icon === '📁' || spaceState.icon === '🔗') {
        spaceState.icon = spaceState.mode === 'linked' ? '🔗' : '📁';
        renderSpaceIcons();
      }
    }
    $$('#spaceMode button').forEach((b) => b.classList.toggle('active', b === btn));
    $('#spaceCreatePane').hidden = spaceState.mode !== 'created';
    $('#spaceLinkPane').hidden = spaceState.mode !== 'linked';
    renderSpacePreview();
  };
  $('#spaceBrowseParent').onclick = () => openDirBrowser('parent', $('#spaceParent').value.trim());
  $('#spaceBrowseLink').onclick = () => openDirBrowser('link', $('#spacePath').value.trim());
  $('#dirUp').onclick = () => spaceState.dirParent && loadDir(spaceState.dirParent);
  $('#dirPick').onclick = pickDir;

  $('#sidebarToggle').onclick = () => $('#app').classList.toggle('collapsed');
  $('#themeToggle').onclick = () => applyTheme(S.theme === 'dark' ? 'light' : 'dark');
  $('#sendBtn').onclick = () => send();
  $('#stopBtn').onclick = stop;
  $('#modelCardBtn').onclick = toggleModel;
  $('#openSettings').onclick = () => openSettings('models');
  $('#openParams').onclick = openParams;
  $('#saveParams').onclick = saveParams;

  const input = $('#input');
  input.addEventListener('input', () => {
    autoGrow(input);
    updateSlashMenu();
  });
  input.addEventListener('keydown', (e) => {
    if (slashKeydown(e)) return;
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });
  // 光标挪回 /token 后面时重新唤起候选
  input.addEventListener('click', () => updateSlashMenu());
  input.addEventListener('blur', () => {
    setTimeout(() => {
      if (!slash.open) return;
      // 焦点还在输入框（比如点了候选）就不关
      if (document.activeElement !== input) closeSlashMenu();
    }, 140);
  });

  // ---- 附件：选择 / 粘贴 / 拖拽 ----
  $('#attachChip').onclick = () => $('#chatFileInput').click();
  $('#chatFileInput').onchange = async (e) => {
    await addFilesToComposer(e.target.files);
    e.target.value = '';
  };
  input.addEventListener('paste', (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    addFilesToComposer(files);
  });
  const composerBox = $('#composerBox');
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  composerBox.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    $('#dropHint').hidden = false;
  });
  composerBox.addEventListener('dragleave', (e) => {
    if (composerBox.contains(e.relatedTarget)) return;
    $('#dropHint').hidden = true;
  });
  composerBox.addEventListener('drop', async (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    $('#dropHint').hidden = true;
    await addFilesToComposer(e.dataTransfer.files);
  });
  // 别让浏览器直接把拖进来的文件当成页面打开
  document.addEventListener('dragover', (e) => {
    if (hasFiles(e)) e.preventDefault();
  });
  document.addEventListener('drop', (e) => {
    if (hasFiles(e)) e.preventDefault();
  });

  // ---- 右侧预览面板：预览 / 源码 切换 + 工具栏 ----
  $$('#viewerMode button').forEach((btn) => {
    btn.onclick = () => setViewerMode(btn.dataset.mode);
  });
  $('#viewerDownload').onclick = () => {
    const tab = activeTab();
    if (!tab) return;
    const a = document.createElement('a');
    a.href = rawUrl(tab.file);
    if (tab.file.name) a.download = tab.file.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };
  $('#viewerNewWindow').onclick = () => {
    const tab = activeTab();
    if (tab) window.open(rawUrl(tab.file), '_blank');
  };
  $('#viewerFolder').onclick = () => revealWorkspace();
  $('#viewerReload').onclick = () => {
    const tab = activeTab();
    if (!tab) return;
    tab.state = 'idle';
    tab.error = '';
    loadViewerTab(tab);
  };
  $('#viewerTabs').addEventListener(
    'wheel',
    (e) => {
      const box = $('#viewerTabs');
      if (!box || box.scrollWidth <= box.clientWidth) return;
      e.preventDefault();
      box.scrollLeft += e.deltaY || e.deltaX;
    },
    { passive: false }
  );
  $('#viewerClose').onclick = () => resetViewer();
  bindViewerResizer();
  // 启动时恢复上次拖拽的面板宽度
  applySidebarWidth(sidebarWidth());
  applyWsWidth(wsWidth());
  window.addEventListener('resize', () => {
    applySidebarWidth(Math.min(sidebarWidth(), sidebarMaxWidth()));
    if (S.workspace.open) applyWsWidth(wsWidth());
    const vp = $('#viewerPanel');
    if (vp && !vp.hidden) applyViewerWidth(viewerWidth());
  });

  $('#searchInput').oninput = (e) => {
    S.search = e.target.value.trim();
    renderSidebar();
  };

  // ---- 模型 ----
  $('#modelChip').onclick = (e) => {
    e.stopPropagation();
    togglePopover('modelMenu');
  };
  $('#manageModels').onclick = (e) => {
    e.stopPropagation();
    closeAllPopovers();
    openSettings('models');
  };

  // ---- 思考模式 ----
  $('#thinkChip').onclick = (e) => {
    e.stopPropagation();
    togglePopover('thinkMenu');
  };
  $('#thinkToggle').onchange = (e) => setThinking(e.target.checked);
  $$('#thinkLevels button').forEach((btn) => {
    btn.onclick = () => setThinkingLevel(btn.dataset.level);
  });

  // ---- 技能 ----
  $('#skillChip').onclick = (e) => {
    e.stopPropagation();
    togglePopover('skillMenu');
  };
  $('#manageSkills').onclick = (e) => {
    e.stopPropagation();
    closeAllPopovers();
    openSettings('skills');
  };
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.composer')) closeAllPopovers();
  });

  // ---- 工作区 ----
  $('#openWorkspace').onclick = () => toggleWorkspace();
  $('#wsClose').onclick = () => toggleWorkspace(false);
  $('#wsRefresh').onclick = () => loadWorkspace();
  $('#wsOpenFolder').onclick = revealWorkspace;
  $('#wsUpload').onclick = () => $('#wsFileInput').click();
  $('#wsFileInput').onchange = async (e) => {
    await uploadToWorkspace(e.target.files || []);
    e.target.value = '';
  };
  $('#exportConv').onclick = exportConversation;
  $('#openUsage').onclick = () => (S.view === 'usage' ? closeUsage() : openUsage());

  // ---- 定时任务 ----
  $('#navSchedules').onclick = openSchedules;
  $('#schedBack').onclick = closeSchedules;
  $('#schedNew').onclick = () => toggleScheduleEditor();
  $('#batchBtn').onclick = () => setBatchMode(!S.batch.on);
  $('#batchCancel').onclick = () => setBatchMode(false);
  $('#batchDelete').onclick = deleteBatchConversations;
  $('#schedRefresh').onclick = () => loadSchedules();
  $('#schParse').onclick = parseScheduleText;
  $('#schNl').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      parseScheduleText();
    }
  });
  $('#schKind').onchange = scheduleKindChanged;
  $('#schSave').onclick = saveSchedule;
  $('#usageRefresh').onclick = loadUsage;
  $('#usageBack').onclick = closeUsage;
  $('#ctxMeter').onclick = openParams;
  $('#compactNow').onclick = async () => {
    await compactNow();
    closeModal('paramsModal');
  };

  // ---- 设置：任务与空间 ----
  $('#settingsNewSpace').onclick = () => openSpaceModal();
  ['wsMaxRead', 'wsMaxTree'].forEach((id) => {
    const input = $(`#${id}`);
    if (!input) return;
    input.onchange = async () => {
      await saveConfig({
        workspace: {
          max_read_bytes: Number($('#wsMaxRead').value),
          max_tree_entries: Number($('#wsMaxTree').value),
        },
      });
      toast('已保存');
    };
  });

  // ---- 设置：MCP ----
  $('#addMcpBtn').onclick = () => openMcpEditor(null);
  $('#mcTransport').onchange = toggleMcpFields;
  $('#saveMcp').onclick = saveMcpServer;
  $('#testMcpBtn').onclick = testMcpServer;
  $('#dbgRefresh').onclick = () => loadDbgTools(true);
  $('#dbgCall').onclick = callDbgTool;
  $('#dbgServer').onchange = async () => {
    resetDbgPanel();
    await loadDbgTools(false);
  };

  // ---- 设置：技能 ----
  $('#addSkillBtn').onclick = () => openSkillEditor(null);
  $('#saveSkill').onclick = saveSkillEditor;

  // ---- 设置：记忆 ----
  $('#memorySave').onclick = saveMemory;
  $('#memoryClear').onclick = async () => {
    if (!confirm('清空全部长期记忆？')) return;
    await api('/api/memory', { method: 'DELETE' });
    await loadMemory();
    renderMemory();
    toast('记忆已清空');
  };

  $('#modelSelect').onchange = async (e) => {
    try {
      await useModel(e.target.value);
    } catch (err) {
      toast(`切换失败：${err.message}`, 3200);
    }
  };

  $('#suggestions').onclick = (e) => {
    const btn = e.target.closest('button[data-prompt]');
    if (!btn) return;
    input.value = btn.dataset.prompt;
    autoGrow(input);
    input.focus();
  };

  // 弹窗关闭
  $$('[data-close]').forEach((btn) => {
    btn.onclick = () => closeModal(btn.dataset.close);
  });
  $$('.modal-mask').forEach((mask) => {
    mask.addEventListener('mousedown', (e) => {
      if (e.target === mask) closeModal(mask.id);
    });
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const mask = $$('.modal-mask').find((m) => !m.hidden);
    if (mask) {
      closeModal(mask.id);
      return;
    }
    // 整页视图也用 Esc 退回对话
    if (S.view === 'usage') closeUsage();
    else if (S.view === 'schedules') closeSchedules();
  });

  // 设置面板
  $$('#settingsTabs .tab').forEach((tab) => {
    tab.onclick = () => {
      switchTab(tab.dataset.tab);
      refreshSettingsTab(tab.dataset.tab);
    };
  });
  $('#addConnBtn').onclick = () => openConnEditor(null);
  $('#addLocalBtn').onclick = () => openModelEditor(null, 'local_hf');
  $('#saveConn').onclick = saveConn;
  $('#cFetchModels').onclick = fetchConnModels;

  // 可搜索多选下拉（连接弹窗 ms* / 模型弹窗 mm*）
  bindPicker('conn');
  bindPicker('model');
  document.addEventListener('mousedown', (e) => {
    Object.keys(PICKERS).forEach((which) => {
      const els = pickerEls(which);
      if (!els.wrap || !els.panel || els.panel.hidden) return;
      if (!els.wrap.contains(e.target) && !els.panel.contains(e.target)) closePickerPanel(which);
    });
  });
  $$('.modal-body').forEach((body) =>
    body.addEventListener(
      'scroll',
      () => Object.keys(PICKERS).forEach((which) => closePickerPanel(which)),
      { passive: true }
    )
  );
  window.addEventListener('resize', () =>
    Object.keys(PICKERS).forEach((which) => closePickerPanel(which))
  );

  $('#mFetchModels').onclick = fetchModelsForModelForm;
  $('#mManualAdd').onclick = addManualModelName;
  $('#mManualName').onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') {
      e.preventDefault();
      addManualModelName();
    }
  };
  $('#hubSave').onclick = saveHub;
  $('#hubSource').onchange = syncHubSource;
  $('#mRepoId').oninput = updateRepoHint;
  $('#mDownload').onclick = startModelDownload;
  $('#mDlCancel').onclick = cancelDownload;
  $('#mLocalMode').onclick = (e) => {
    const btn = e.target.closest('[data-mode]');
    if (btn) setLocalMode(btn.dataset.mode);
  };
  $('#mProvider').onchange = toggleProviderFields;
  $('#mConn').onchange = () => {
    const connId = $('#mConn').value;
    const current = S.editingModelId ? configModel(S.editingModelId) : null;
    resetModelPicker(connId, current);
  };
  $('#saveModel').onclick = saveModel;

  // 性能面板
  $('#mcpInline').onchange = async (e) => {
    try {
      await saveConfig({ mcp: { inline_tools: e.target.checked } });
      toast(e.target.checked ? 'MCP 工具已内联，下一轮生效' : 'MCP 工具改为按需发现');
      await loadPerf();
    } catch (err) {
      toast(`保存失败：${err.message}`, 3200);
    }
  };
  $('#perfSaveBtn').onclick = savePerf;
  $('#perfResetBtn').onclick = async () => {
    try {
      await api('/api/perf/reset', { method: 'POST' });
      await loadPerf();
      toast('前缀缓存已清空');
    } catch (err) {
      toast(`清空失败：${err.message}`, 3200);
    }
  };
  $('#perfWarmBtn').onclick = async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = '预热中…';
    try {
      const r = await api('/api/perf/warm', { method: 'POST' });
      toast(
        r.ok
          ? `已预热 ${r.tokens} token，耗时 ${r.seconds}s${r.partial ? '（被对话打断，已算完的部分可用）' : ''}`
          : `预热未完成：${r.detail || ''}`,
        3200,
      );
      await loadPerf();
    } catch (err) {
      toast(`预热失败：${err.message}`, 3200);
    } finally {
      btn.disabled = false;
      btn.textContent = '预热前缀';
    }
  };

  $('#prefTemp').oninput = (e) => ($('#prefTempVal').textContent = Number(e.target.value).toFixed(2));  $('#prefTopP').oninput = (e) => ($('#prefTopPVal').textContent = Number(e.target.value).toFixed(2));
  $('#prefThreshold').oninput = (e) => ($('#prefThresholdVal').textContent = Number(e.target.value).toFixed(2));
  $('#convTemp').oninput = (e) => ($('#convTempVal').textContent = Number(e.target.value).toFixed(2));
  $('#convTopP').oninput = (e) => ($('#convTopPVal').textContent = Number(e.target.value).toFixed(2));

  // 设置面板任意改动自动保存偏好（防抖）
  let prefTimer = null;
  const prefHandler = () => {
    clearTimeout(prefTimer);
    prefTimer = setTimeout(() => {
      if ($('#settingsModal').hidden) return;
      if ($('.tab-panel[data-panel="prefs"]').classList.contains('active')) savePreferences();
    }, 650);
  };
  ['#prefSystem', '#prefTemp', '#prefTopP', '#prefMaxTokens', '#prefMaxRounds', '#prefTools', '#prefAutoCompact', '#prefCompactMode', '#prefCompactTokens', '#prefCompactOnOverflow', '#prefThreshold', '#prefKeepRecent'].forEach((sel) => {
    $(sel).addEventListener('change', prefHandler);
    $(sel).addEventListener('input', prefHandler);
  });
  $('#prefShell').addEventListener('change', async () => {
    await savePreferences();
    await Promise.all([loadTools(), loadMcp()]);
    renderToolGrid();
  });

  // 快捷键
  document.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      createConversation().then(() => $('#input').focus());    } else if (mod && e.key.toLowerCase() === 'j') {
      e.preventDefault();
      const state = thinkState();
      setThinking(!state.thinking);
    } else if (mod && e.shiftKey && e.key.toLowerCase() === 'o') {
      e.preventDefault();
      toggleWorkspace();
    } else if (e.key === 'Escape') {
      closeAllPopovers();
    }
  });
}

/* ---------------------------- 思考 / 技能 / 工作区 / MCP / 记忆 ---------------------------- */

function thinkState() {
  if (S.conv) return { thinking: !!S.conv.thinking, level: S.conv.thinking_level || 'medium' };
  S.pendingThink = S.pendingThink || {
    thinking: !!(S.config?.defaults?.thinking),
    level: S.config?.defaults?.thinking_level || 'medium',
  };
  return S.pendingThink;
}

async function setThinking(on) {
  const state = thinkState();
  state.thinking = on;
  if (S.conv) {
    S.conv = await api(`/api/conversations/${S.conv.id}`, { method: 'PATCH', body: { thinking: on } });
  }
  updateHeader();
}

async function setThinkingLevel(level) {
  const state = thinkState();
  state.level = level;
  if (S.conv) {
    S.conv = await api(`/api/conversations/${S.conv.id}`, {
      method: 'PATCH',
      body: { thinking_level: level, thinking: true },
    });
  } else {
    state.thinking = true;
  }
  updateHeader();
}

function togglePopover(id) {
  const target = $(`#${id}`);
  const willOpen = target.hidden;
  ['modelMenu', 'thinkMenu', 'skillMenu', 'slashMenu'].forEach((other) => {
    const node = $(`#${other}`);
    if (node) node.hidden = true;
  });
  if (id !== 'slashMenu') slash.open = false;
  target.hidden = !willOpen;
  if (id === 'skillMenu' && willOpen) renderSkillMenu();
}

async function loadSkills() {
  const data = await api('/api/skills');
  S.skills = data.skills || [];
  $('#skillsPath').textContent = data.dir || S.config?.paths?.skills || '';
}

/* 模型的 skill_create / skill_delete 会直接改磁盘：前端这份 S.skills 必须跟着刷，
   否则「技能」菜单和 / 候选里还挂着已删除的技能，选它就只会得到「技能不存在」。
   delay 用于合并同一轮里的连续变更（模型可能连着写好几个文件）。 */
let skillsRefreshTimer = null;
function refreshSkillsSoon(delay = 500) {
  clearTimeout(skillsRefreshTimer);
  skillsRefreshTimer = setTimeout(async () => {
    try {
      await loadSkills();
    } catch (_) {
      return;
    }
    // 会话里可能残留已删技能的 slug：本地也清掉，勾选状态才和列表一致
    if (S.conv) {
      const alive = new Set(S.skills.map((s) => s.slug));
      const before = S.conv.active_skills || [];
      const after = before.filter((n) => alive.has(n));
      if (after.length !== before.length) {
        S.conv.active_skills = after;
        updateHeader();
      }
    }
    renderSkillMenu();
    const modal = $('#settingsModal');
    if (modal && !modal.hidden) renderSkillList();
  }, delay);
}

function activeSkillNames() {
  if (S.conv) return [...(S.conv.active_skills || [])];
  S.pendingSkills = S.pendingSkills || [];
  return [...S.pendingSkills];
}

async function toggleSkill(name, on) {
  const names = new Set(activeSkillNames());
  if (on) names.add(name);
  else names.delete(name);
  const list = [...names];
  if (S.conv) {
    S.conv = await api(`/api/conversations/${S.conv.id}`, {
      method: 'PATCH',
      body: { active_skills: list },
    });
  } else {
    S.pendingSkills = list;
  }
  updateHeader();
  renderSkillMenu();
}

function renderSkillMenu() {
  const box = $('#skillMenuList');
  if (!box) return;
  const active = new Set(activeSkillNames());
  if (!S.skills.length) {
    box.innerHTML = '<div class="ws-empty">还没有技能<br/>在「设置 → 技能」里新建，或让模型帮你生成</div>';
    return;
  }
  box.innerHTML = '';
  S.skills.forEach((skill) => {
    const row = el('label', 'skill-menu-item');
    row.innerHTML = `
      <input type="checkbox" ${active.has(skill.slug) ? 'checked' : ''} ${skill.enabled ? '' : 'disabled'} />
      <div>${esc(skill.name || skill.slug)}${skill.enabled ? '' : '（已停用）'}
        <span>${esc(skill.description || '')}</span>
      </div>`;
    row.querySelector('input').onchange = (e) => toggleSkill(skill.slug, e.target.checked);
    box.appendChild(row);
  });
}

/* ---------------- 输入框 / 调起技能 ----------------
   在输入框里敲 / 会弹出技能候选（按 slug / 名称 / 描述过滤），
   上下键 + Enter 选择，选中后把 /slug 留在原位置——
   发送时后端会把它改写成「【使用技能：slug】」注入到那句话的语义位置上。 */

const slash = { open: false, items: [], index: 0, start: -1, loading: false };

function slashTokenAt(text, caret) {
  const before = text.slice(0, caret);
  // 注意三点：
  // 1) 「/」后面允许还没输入任何字符——敲下 / 就要立刻弹候选，不能逼用户再打字母；
  // 2) 中文输入法常把 / 打成「、」，也兼容全角「／」；
  // 3) 中文后面直接跟 / 也认（中文句子一般没有空格），但英文/路径前后不触发，
  //    避免 src/app.py、http:// 这类误弹。
  const m = before.match(/(^|[\s\u4e00-\u9fff])(?:\/|／|、)([a-z0-9][a-z0-9_\-]*)?$/);
  if (!m) return null;
  const query = m[2] || '';
  return { start: before.length - query.length - 1, query: query.toLowerCase() };
}

function updateSlashMenu() {
  const input = $('#input');
  const token = slashTokenAt(input.value, input.selectionStart ?? input.value.length);
  if (!token) {
    closeSlashMenu();
    return;
  }
  const q = token.query;
  // 敲下 / 的那一刻 S.skills 还没加载过（比如页面刚打开）就先拉一次
  if (!S.skills.length && !slash.loading) {
    slash.loading = true;
    loadSkills()
      .catch(() => {})
      .finally(() => {
        slash.loading = false;
        if ($('#input').value.includes('/') || $('#input').value.includes('、')) updateSlashMenu();
      });
  }
  const items = S.skills
    .filter((s) => s.enabled !== false)
    .filter(
      (s) =>
        !q ||
        s.slug.toLowerCase().includes(q) ||
        String(s.name || '').toLowerCase().includes(q) ||
        String(s.description || '').toLowerCase().includes(q)
    )
    .slice(0, 8);
  if (!items.length) {
    closeSlashMenu();
    return;
  }
  const changed = JSON.stringify(items.map((s) => s.slug)) !== JSON.stringify(slash.items.map((s) => s.slug));
  Object.assign(slash, { open: true, items, index: changed ? 0 : Math.min(slash.index, items.length - 1), start: token.start });
  renderSlashMenu();
  $('#slashMenu').hidden = false;
}

function renderSlashMenu() {
  const box = $('#slashList');
  if (!box) return;
  box.replaceChildren(
    ...slash.items.map((s, i) => {
      const row = el('button', 'slash-item' + (i === slash.index ? ' active' : ''));
      row.type = 'button';
      row.innerHTML = `
        <span class="slash-title"><b>${esc(s.name || s.slug)}</b><span class="slash-slug">/${esc(s.slug)}</span></span>
        <span class="slash-desc">${esc(s.description || '')}</span>`;
      row.onmousedown = (e) => {
        e.preventDefault();
        pickSlash(i);
      };
      row.onmouseenter = () => {
        if (slash.index !== i) {
          slash.index = i;
          renderSlashMenu();
        }
      };
      return row;
    })
  );
}

function pickSlash(i) {
  const skill = slash.items[i];
  if (!skill) return;
  const input = $('#input');
  const end = input.selectionStart ?? input.value.length;
  const head = input.value.slice(0, slash.start);
  const tail = input.value.slice(end);
  const inserted = `/${skill.slug} `;
  input.value = head + inserted + tail.replace(/^\s+/, '');
  const pos = head.length + inserted.length;
  input.setSelectionRange(pos, pos);
  closeSlashMenu();
  autoGrow(input);
  input.focus();
}

function closeSlashMenu() {
  slash.open = false;
  const menu = $('#slashMenu');
  if (menu) menu.hidden = true;
}

function slashKeydown(e) {
  if (!slash.open) return false;
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    slash.index = (slash.index + 1) % slash.items.length;
    renderSlashMenu();
    return true;
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    slash.index = (slash.index - 1 + slash.items.length) % slash.items.length;
    renderSlashMenu();
    return true;
  }
  if ((e.key === 'Enter' || e.key === 'Tab') && !e.isComposing) {
    e.preventDefault();
    pickSlash(slash.index);
    return true;
  }
  if (e.key === 'Escape') {
    e.preventDefault();
    closeSlashMenu();
    return true;
  }
  return false;
}

/* ---------------- 工作区 ---------------- */

async function toggleWorkspace(force, remember = true) {
  const open = force == null ? !S.workspace.open : force;
  S.workspace.open = open;
  $('#workspacePanel').hidden = !open;
  if (remember) localStorage.setItem('lm-ws-open', open ? '1' : '0');
  // 文件树和预览同时开着时，重新按可用空间收一下预览宽度，别把对话挤没了
  if (open && !$('#viewerPanel').hidden) applyViewerWidth(viewerWidth());
  if (open) await loadWorkspace();
}

async function loadWorkspace() {
  try {
    const data = await api(withScope('/api/workspace/tree'));
    S.workspace.entries = data.entries || [];
    S.workspace.root = data.root || '';
    renderWorkspaceList(data);
  } catch (err) {
    $('#wsList').innerHTML = `<div class="ws-empty">读取失败：${esc(err.message)}</div>`;
  }
}

function renderWorkspaceList(data = {}) {
  const box = $('#wsList');
  const root = S.workspace.root || data.root || '';
  $('#wsRoot').textContent = root;
  $('#wsRoot').title = root;
  const conv = S.conv;
  const scope = currentScope();
  $('#wsScopeIcon').textContent = conv ? '💬' : scope.icon || '📁';
  $('#wsScopeName').textContent = conv ? conv.title || '对话' : scope.type === 'space' ? scope.name : '默认空间';
  $('#wsStat').textContent = `${S.workspace.entries.length} 个文件`;
  box.innerHTML = '';
  if (!S.workspace.entries.length) {
    box.innerHTML = conv
      ? '<div class="ws-empty">这个对话还没生成文件<br/>让模型写点东西，或点上方「上传 / 新建」</div>'
      : '<div class="ws-empty">这个目录里还没有文件</div>';
    return;
  }
  const tree = buildFileTree(S.workspace.entries);
  box.appendChild(renderTreeLevel(tree, 0));
}

function buildFileTree(entries) {
  const root = { dirs: new Map(), files: [] };
  entries.forEach((entry) => {
    const parts = String(entry.path).split('/').filter(Boolean);
    if (!parts.length) return;
    let node = root;
    for (let i = 0; i < parts.length - 1; i += 1) {
      const name = parts[i];
      if (!node.dirs.has(name)) {
        node.dirs.set(name, { name, path: parts.slice(0, i + 1).join('/'), dirs: new Map(), files: [] });
      }
      node = node.dirs.get(name);
    }
    node.files.push({ ...entry, name: parts[parts.length - 1] });
  });
  return root;
}

function renderTreeLevel(node, depth) {
  const wrap = el('div', 'ws-level');
  const dirs = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name, 'zh'));
  const files = [...node.files].sort((a, b) => a.name.localeCompare(b.name, 'zh'));
  dirs.forEach((dir) => {
    const open = S.wsExpanded.has(dir.path);
    const row = el('div', 'ws-row ws-dir');
    row.style.paddingLeft = `${8 + depth * 14}px`;
    row.innerHTML = `
      <span class="ws-caret">${open ? '▾' : '▸'}</span>
      <span class="ico">${open ? '📂' : '📁'}</span>
      <span class="name">${esc(dir.name)}</span>
      <span class="size">${dir.files.length + dir.dirs.size}</span>`;
    row.onclick = () => {
      if (S.wsExpanded.has(dir.path)) S.wsExpanded.delete(dir.path);
      else S.wsExpanded.add(dir.path);
      renderWorkspaceList({ root: S.workspace.root });
    };
    wrap.appendChild(row);
    if (open) wrap.appendChild(renderTreeLevel(dir, depth + 1));
  });
  files.forEach((file) => {
    const kind = guessKind(file.path);
    const row = el('div', 'ws-row ws-file');
    row.style.paddingLeft = `${8 + depth * 14 + 14}px`;
    row.innerHTML = `
      <span class="ico">${kindMeta({ kind }).icon}</span>
      <span class="name">${esc(file.name)}</span>
      <span class="size">${fmtSize(file.size)}</span>`;
    row.onclick = () =>
      openPreview({
        path: file.path,
        name: file.name,
        size: file.size,
        kind,
        url: withScope(`/api/workspace/raw?path=${encodeURIComponent(file.path)}`),
      });
    wrap.appendChild(row);
  });
  return wrap;
}

function renderSpaceManage() {
  const box = $('#spaceManageList');
  if (!box) return;
  box.innerHTML = '';
  const normal = S.spaces.filter((s) => (s.kind || 'space') !== 'schedule');
  if (S.taskScope) box.appendChild(spaceManageRow(S.taskScope, true));
  normal.forEach((space) => box.appendChild(spaceManageRow(space, false)));
  if (!normal.length) {
    box.appendChild(
      el(
        'div',
        'space-manage-empty',
        '还没有空间。空间适合放「需要长期复用的一组文件」：一个代码仓库、一个客户资料目录，或者单独建的草稿文件夹。'
      )
    );
  }
}

function spaceManageRow(item, isTask) {
  const row = el('div', `space-manage-row${isTask ? ' is-task' : ''}`);
  const count = item.conversation_count || 0;
  row.innerHTML = `
    <span class="sm-ico">${esc(item.icon || (isTask ? '⚡' : '📁'))}</span>
    <div class="sm-body">
      <div class="sm-name">${esc(item.name)}${isTask ? '' : `<span class="sm-tag">${item.mode === 'linked' ? '关联目录' : '新建文件夹'}</span>`}</div>
      <div class="sm-path" title="${esc(item.path)}">${esc(item.path || '')}</div>
    </div>
    <span class="sm-count">${count ? `${count} 个对话` : ''}</span>
    <div class="sm-ops">
      <button data-act="open">打开</button>
      ${isTask ? '' : '<button data-act="edit">编辑</button>'}
      ${isTask ? '' : '<button data-act="remove" class="danger">移除</button>'}
    </div>`;
  row.querySelector('[data-act="open"]').onclick = () => revealPath(item.path);
  const edit = row.querySelector('[data-act="edit"]');
  if (edit) edit.onclick = () => openSpaceModal(item);
  const remove = row.querySelector('[data-act="remove"]');
  if (remove) {
    remove.onclick = async () => {
      await removeSpace(item);
      renderSpaceManage();
    };
  }
  return row;
}

async function loadWorkspaceSummary() {
  try {
    const info = await api(withScope('/api/workspace/info'));
    const stat = info.stats || {};
    const scope = currentScope();
    $('#wsSummary').innerHTML = `当前工作位置：<b>${esc(scope.type === 'space' ? scope.name : '任务')}</b> · <code>${esc(info.root)}</code><br/>共 ${stat.files || 0} 个文件，占用 ${fmtSize(stat.bytes || 0)}`;
  } catch (_) {}
}

async function revealWorkspace() {
  try {
    await api(withScope('/api/workspace/reveal'), { method: 'POST', body: {} });
    toast('已在文件管理器中打开');
  } catch (err) {
    toast(`打开失败：${err.message}`);
  }
}

async function uploadToWorkspace(files) {
  for (const file of files) {
    const isText = /\.(md|txt|json|csv|tsv|py|js|ts|html|css|yml|yaml|xml|log|sh|bat|ini|toml)$/i.test(file.name);
    try {
      if (isText) {
        const text = await file.text();
        await api(withScope('/api/workspace/file'), { method: 'PUT', body: { path: file.name, content: text } });
      } else {
        const buf = await file.arrayBuffer();
        let binary = '';
        const bytes = new Uint8Array(buf);
        for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
        await api(withScope('/api/workspace/upload'), {
          method: 'POST',
          body: { path: file.name, data: btoa(binary) },
        });
      }
      toast(`已上传 ${file.name}`);
    } catch (err) {
      toast(`上传 ${file.name} 失败：${err.message}`, 3000);
    }
  }
  if (S.workspace.open) loadWorkspace();
}

/* ---------------- 附件（多模态输入） ---------------- */

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const bytes = new Uint8Array(reader.result);
      let binary = '';
      const chunk = 0x8000;
      for (let i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
      }
      resolve(btoa(binary));
    };
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.readAsArrayBuffer(file);
  });
}

async function addFilesToComposer(fileList) {
  const files = [...(fileList || [])].filter(Boolean);
  if (!files.length) return;
  S.pendingUploading += files.length;
  renderPendingAttachments();
  for (const file of files) {
    try {
      if (file.size > 30 * 1024 * 1024) throw new Error('单个文件不要超过 30MB');
      const data = await fileToBase64(file);
      const meta = await api(withScope('/api/uploads'), { method: 'POST', body: { name: file.name, data } });
      S.pendingAttachments.push(meta);
    } catch (err) {
      toast(`添加 ${file.name} 失败：${err.message}`, 3200);
    } finally {
      S.pendingUploading = Math.max(0, S.pendingUploading - 1);
      renderPendingAttachments();
    }
  }
}

function renderPendingAttachments() {
  const box = $('#pendingAttachments');
  if (!box) return;
  const files = S.pendingAttachments;
  const uploading = S.pendingUploading > 0;
  if (!files.length && !uploading) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  box.hidden = false;
  box.innerHTML = '';
  files.forEach((file, idx) => {
    const meta = kindMeta(file);
    const chip = el('div', 'pending-chip');
    const thumb =
      file.kind === 'image'
        ? `<img src="${rawUrl(file)}" alt="" />`
        : `<span class="pending-ico">${meta.icon}</span>`;
    chip.innerHTML = `${thumb}<div class="pending-info"><b>${esc(file.name)}</b><span>${meta.label} · ${fmtSize(file.size)}</span></div>`;
    const del = el('button', 'pending-del', '×');
    del.title = '移除';
    del.onclick = () => {
      S.pendingAttachments.splice(idx, 1);
      renderPendingAttachments();
    };
    chip.appendChild(del);
    box.appendChild(chip);
  });
  if (uploading) {
    const chip = el('div', 'pending-chip uploading');
    chip.innerHTML = '<span class="pending-ico"><span class="spinner"></span></span><div class="pending-info"><b>正在上传…</b></div>';
    box.appendChild(chip);
  }
}

function clearPendingAttachments() {
  S.pendingAttachments = [];
  renderPendingAttachments();
}

/* ---------------- 文件预览 ---------------- */

const PREVIEW_TEXT_KINDS = ['markdown', 'html', 'table', 'code', 'text'];

const HLJS_LANG = {
  py: 'python', js: 'javascript', mjs: 'javascript', ts: 'typescript', tsx: 'typescript',
  jsx: 'javascript', json: 'json', yaml: 'yaml', yml: 'yaml', html: 'xml', htm: 'xml',
  xml: 'xml', css: 'css', scss: 'scss', sh: 'bash', bat: 'dos', ps1: 'powershell',
  sql: 'sql', md: 'markdown', markdown: 'markdown', csv: 'plaintext', tsv: 'plaintext',
  go: 'go', rs: 'rust', java: 'java', c: 'c', h: 'c', cpp: 'cpp', rb: 'ruby', php: 'php',
  toml: 'ini', ini: 'ini', cfg: 'ini', txt: 'plaintext', log: 'plaintext',
};

function hljsLang(name) {
  const ext = String(name || '').split('.').pop().toLowerCase();
  return HLJS_LANG[ext] || '';
}

/* ---------------- 右侧分屏文件查看器（多标签） ---------------- */

const viewer = { tabs: [], activeId: null, seq: 0, treeAutoClosed: false };

function viewerTab(id) {
  return viewer.tabs.find((t) => t.id === id) || null;
}

function activeTab() {
  return viewerTab(viewer.activeId);
}

function viewerMaxWidth() {
  const app = $('#app');
  const sidebar = app.classList.contains('collapsed')
    ? 0
    : $('#sidebar').getBoundingClientRect().width || 274;
  const tree = S.workspace.open
    ? $('#workspacePanel').getBoundingClientRect().width || 320
    : 0;
  // 给对话区至少留 380px
  return Math.max(340, window.innerWidth - sidebar - tree - 380);
}

function viewerWidth() {
  const saved = Number(localStorage.getItem('lm-viewer-width'));
  const want = saved || Math.round(Math.min(860, Math.max(460, window.innerWidth * 0.42)));
  return Math.min(Math.max(340, want), viewerMaxWidth());
}

function applyViewerWidth(width) {
  const panel = $('#viewerPanel');
  if (!panel) return;
  panel.style.flexBasis = `${width}px`;
  panel.style.width = `${width}px`;
}

function showViewer() {
  const panel = $('#viewerPanel');
  if (!panel || !panel.hidden) return;
  // 屏幕太窄时先收起左侧文件树，把空间留给对话和预览
  if (window.innerWidth < 1500 && S.workspace.open) {
    viewer.treeAutoClosed = true;
    toggleWorkspace(false, false);
  }
  panel.hidden = false;
  applyViewerWidth(viewerWidth());
}

function hideViewer() {
  const panel = $('#viewerPanel');
  if (panel) panel.hidden = true;
  if (viewer.treeAutoClosed) {
    viewer.treeAutoClosed = false;
    toggleWorkspace(true, false);
  }
}

/** 切换会话 / 空间后，旧标签的文件路径已经不在同一个根目录下了，直接清空 */
function resetViewer() {
  if (!viewer.tabs.length && $('#viewerPanel')?.hidden !== false) return;
  viewer.tabs = [];
  viewer.activeId = null;
  const body = $('#viewerBody');
  if (body) body.innerHTML = '';
  renderViewerTabs();
  renderViewerChrome();
  hideViewer();
}

async function openPreview(file) {
  const path = file.path || file.name;
  if (!path) return;
  const kind = file.kind || guessKind(path);
  let tab = viewer.tabs.find((t) => t.file.path === path);
  if (!tab) {
    tab = {
      id: `v${++viewer.seq}`,
      file: { ...file, path, kind },
      kind,
      text: '',
      truncated: false,
      error: '',
      mode: 'preview',
      state: 'idle',
      scroll: 0,
    };
    viewer.tabs.push(tab);
  } else {
    const changed = file.size != null && tab.file.size != null && Number(tab.file.size) !== Number(file.size);
    tab.file = { ...tab.file, ...file, path, kind };
    tab.kind = kind;
    // 文件被重新生成过（体积变了）就悄悄重新渲染一次
    if (changed) {
      tab.state = 'idle';
      tab.text = '';
      tab.error = '';
      tab.docNote = '';
      if (tab.pdfUrl) {
        URL.revokeObjectURL(tab.pdfUrl);
        tab.pdfUrl = '';
      }
    }
  }
  showViewer();
  selectViewerTab(tab.id);
}

function selectViewerTab(id) {
  const tab = viewerTab(id);
  if (!tab) return;
  const body = $('#viewerBody');
  const prev = activeTab();
  if (prev && prev.id !== id) prev.scroll = body.scrollTop;
  viewer.activeId = id;
  renderViewerTabs();
  renderViewerChrome();
  if (tab.state === 'idle') loadViewerTab(tab);
  else {
    renderViewerBody();
    body.scrollTop = tab.scroll || 0;
  }
}

async function loadViewerTab(tab) {
  tab.state = 'loading';
  tab.error = '';
  tab.loadingText = '';
  if (viewer.activeId === tab.id) renderViewerBody();

  // pdf 用浏览器自带阅读器；docx/pptx/xlsx 先让本机 Office 渲染成 PDF 再内嵌，
  // 这样右侧看到的就是原文件的排版，而不是把内容抽出来重排
  if (tab.kind === 'document') {
    const name = String(tab.file.name || tab.file.path || '');
    if (/\.pdf$/i.test(name)) {
      tab.state = 'ready';
      if (viewer.activeId === tab.id) renderViewerBody();
      return;
    }
    const force = tab.forceNext ? '&refresh=1' : '';
    tab.forceNext = false;
    tab.loadingText = '正在用本机 Office 渲染这一页，首次打开要几秒…';
    if (viewer.activeId === tab.id) renderViewerBody();
    const url = withScope(`/api/workspace/pdf?path=${encodeURIComponent(tab.file.path)}${force}`);
    try {
      const res = await fetch(url, { cache: 'no-store' });
      if (res.ok && (res.headers.get('content-type') || '').includes('pdf')) {
        const blob = await res.blob();
        if (tab.pdfUrl) URL.revokeObjectURL(tab.pdfUrl);
        tab.pdfUrl = URL.createObjectURL(blob);
        tab.docNote = '';
      } else {
        let detail = res.statusText;
        try {
          detail = (await res.json()).detail || detail;
        } catch (_) {}
        tab.docNote = detail || '渲染失败';
        if (tab.pdfUrl) {
          URL.revokeObjectURL(tab.pdfUrl);
          tab.pdfUrl = '';
        }
      }
    } catch (err) {
      tab.docNote = err.message || '渲染失败';
    }
    tab.state = 'ready';
    if (viewer.activeId === tab.id) renderViewerBody();
    return;
  }

  if (!PREVIEW_TEXT_KINDS.includes(tab.kind)) {
    tab.state = 'ready';
    if (viewer.activeId === tab.id) renderViewerBody();
    return;
  }
  try {
    // 技能库文件在工作目录之外，走 /api/skills/file 通道
    const isSkill = String(tab.file.path).startsWith('skills/');
    const url = isSkill
      ? `/api/skills/file?path=${encodeURIComponent(tab.file.path)}&max_bytes=2000000`
      : withScope(`/api/workspace/file?path=${encodeURIComponent(tab.file.path)}&max_bytes=2000000`);
    const data = await api(url);
    if (data.binary) {
      tab.error = `这是二进制文件（${fmtSize(data.size)}），不支持预览`;
      tab.state = 'error';
    } else {
      tab.text = data.content || '';
      tab.truncated = !!data.truncated;
      tab.state = 'ready';
    }
  } catch (err) {
    tab.error = `读取失败：${err.message}`;
    tab.state = 'error';
  }
  if (viewer.activeId === tab.id) {
    renderViewerChrome();
    renderViewerBody();
  }
}

function closeViewerTab(id) {
  const idx = viewer.tabs.findIndex((t) => t.id === id);
  if (idx < 0) return;
  const closing = viewer.tabs[idx];
  if (closing.pdfUrl) URL.revokeObjectURL(closing.pdfUrl); // 释放渲染结果的 blob
  viewer.tabs.splice(idx, 1);
  if (viewer.activeId !== id) {
    renderViewerTabs();
    return;
  }
  const next = viewer.tabs[idx] || viewer.tabs[idx - 1] || null;
  viewer.activeId = next ? next.id : null;
  if (!next) {
    $('#viewerBody').innerHTML = '';
    renderViewerTabs();
    renderViewerChrome();
    hideViewer();
    return;
  }
  renderViewerTabs();
  renderViewerChrome();
  if (next.state === 'idle') loadViewerTab(next);
  else renderViewerBody();
}

function renderViewerTabs() {
  const box = $('#viewerTabs');
  if (!box) return;
  box.innerHTML = '';
  viewer.tabs.forEach((tab) => {
    const node = el('div', `viewer-tab${tab.id === viewer.activeId ? ' active' : ''}`);
    node.dataset.id = tab.id;
    node.title = tab.file.path;
    const icon = tab.state === 'error' ? '⚠' : kindMeta({ kind: tab.kind }).icon;
    node.innerHTML = `
      <span class="vt-ico">${icon}</span>
      <span class="vt-name">${esc(tab.file.name || tab.file.path)}</span>
      <button class="vt-close" title="关闭这个文件">✕</button>`;
    node.onclick = (e) => {
      if (e.target.closest('.vt-close')) return;
      selectViewerTab(tab.id);
    };
    node.querySelector('.vt-close').onclick = (e) => {
      e.stopPropagation();
      closeViewerTab(tab.id);
    };
    node.onauxclick = (e) => {
      if (e.button !== 1) return;
      e.preventDefault();
      closeViewerTab(tab.id);
    };
    box.appendChild(node);
  });
}

function renderViewerChrome() {
  const tab = activeTab();
  const pathNode = $('#viewerPath');
  const modeNode = $('#viewerMode');
  if (!pathNode || !modeNode) return;
  if (!tab) {
    pathNode.textContent = '';
    pathNode.title = '';
    modeNode.hidden = true;
    return;
  }
  const meta = kindMeta({ kind: tab.kind });
  pathNode.textContent = `${tab.file.path} · ${meta.label} · ${fmtSize(tab.file.size)}`;
  pathNode.title = tab.file.path;
  const needsText = PREVIEW_TEXT_KINDS.includes(tab.kind);
  modeNode.hidden = !needsText;
  $$('#viewerMode button').forEach((btn) => btn.classList.toggle('on', btn.dataset.mode === tab.mode));
}

function setViewerMode(mode) {
  const tab = activeTab();
  if (!tab) return;
  tab.mode = mode;
  tab.scroll = 0;
  renderViewerChrome();
  renderViewerBody();
  $('#viewerBody').scrollTop = 0;
}

function renderViewerBody() {
  const body = $('#viewerBody');
  if (!body) return;
  const tab = activeTab();
  if (!tab) {
    body.innerHTML = '';
    return;
  }
  const { file, kind, text, mode } = tab;

  if (tab.state === 'loading') {
    body.innerHTML = `<div class="preview-note">${esc(tab.loadingText || '加载中…')}</div>`;
    return;
  }

  if (tab.error) {
    body.innerHTML = `<div class="preview-note">${esc(tab.error)}<br/>可以点右上角按钮下载或在新窗口打开。</div>`;
    return;
  }

  if (kind === 'image') {
    body.innerHTML = `<img src="${rawUrl(file)}" alt="${esc(file.name || '')}" />`;
    return;
  }

  if (kind === 'html' && mode === 'preview') {
    body.innerHTML = '';
    const frame = el('iframe', 'preview-frame');
    // 沙箱里跑：脚本可以执行，但拿不到本页的 localStorage / 接口权限
    frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-popups allow-modals');
    const src = `/ws/${String(file.path)
      .split('/')
      .map((part) => encodeURIComponent(part))
      .join('/')}`;
    // 和接口一样带上会话 / 空间参数，否则会落到默认根目录去找文件
    frame.src = withScope(src);
    body.appendChild(frame);
    return;
  }

  if (kind === 'markdown' && mode === 'preview') {
    body.innerHTML = '';
    if (tab.truncated) {
      body.insertAdjacentHTML('beforeend', '<div class="preview-note inline">文件较大，仅显示前一部分</div>');
    }
    const article = el('div', 'md preview-md');
    article.innerHTML = mdToHtml(text || '（空文件）');
    enhance(article);
    body.appendChild(article);
    return;
  }

  if (kind === 'table' && mode === 'preview') {
    body.innerHTML = '';
    body.appendChild(csvTable(text || ''));
    return;
  }

  // 二进制文档：pdf 浏览器直接显示；docx/pptx/xlsx 用本机 Office 渲染好的 PDF 显示
  // （这一类不走文本接口，tab.text 永远是空的，所以必须先单独处理，
  //   否则会掉到下面的文本分支里显示成「（空文件）」）
  if (kind === 'document') {
    body.innerHTML = '';
    const name = String(file.name || file.path || '');
    const meta = kindMeta({ kind });
    if (/\.pdf$/i.test(name)) {
      const frame = el('iframe', 'preview-frame');
      frame.src = rawUrl(file);
      body.appendChild(frame);
      return;
    }
    if (tab.pdfUrl) {
      const frame = el('iframe', 'preview-frame');
      frame.src = tab.pdfUrl;
      body.appendChild(frame);
      return;
    }
    const card = el('div', 'preview-doc');
    card.appendChild(el('div', 'preview-doc-icon', meta.icon));
    card.appendChild(el('div', 'preview-doc-name', esc(name)));
    const hint = tab.docNote
      ? `渲染失败：${esc(tab.docNote)}<br/>可以点右上角 ⤓ 下载，用本机 Office 打开。`
      : `${meta.label} · ${fmtSize(file.size)}<br/>面板里显示不了这个格式。`;
    card.appendChild(el('div', 'preview-doc-hint', hint));

    const actions = el('div', 'preview-doc-actions');
    const retry = el('button', 'btn ghost tiny', '重新渲染');
    retry.onclick = () => {
      tab.state = 'idle';
      tab.docNote = '';
      tab.forceNext = true;
      loadViewerTab(tab);
    };
    const dl = el('button', 'btn ghost tiny', '下载文件');
    dl.onclick = () => {
      const a = document.createElement('a');
      a.href = rawUrl(file);
      if (name) a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    };
    actions.appendChild(retry);
    actions.appendChild(dl);
    card.appendChild(actions);
    body.appendChild(card);
    return;
  }

  // 代码 / 文本 / 查看源码
  body.innerHTML = '';
  if (tab.truncated) {
    body.insertAdjacentHTML('beforeend', '<div class="preview-note inline">文件较大，仅显示前一部分</div>');
  }
  const pre = el('pre', 'preview-code');
  const code = el('code');
  const lang = hljsLang(file.name);
  if (lang) code.className = `language-${lang}`;
  code.textContent = text || '（空文件）';
  pre.appendChild(code);
  body.appendChild(pre);
  if (window.hljs && lang && lang !== 'plaintext') {
    try {
      hljs.highlightElement(code);
    } catch (_) {}
  }
}

function bindViewerResizer() {
  bindColResizer({
    handleId: 'viewerResizer',
    panelId: 'viewerPanel',
    cls: 'viewer-resizing',
    storageKey: 'lm-viewer-width',
    grow: -1, // 面板在右侧：鼠标左移 = 变宽
    min: () => 360,
    max: viewerMaxWidth,
    apply: applyViewerWidth,
  });
  bindSidebarResizer();
  bindWsResizer();
}

/* 通用列拖拽：面板宽度实时跟随鼠标，松手后存 localStorage */
function bindColResizer({ handleId, panelId, cls, storageKey, grow, min, max, apply }) {
  const handle = $(`#${handleId}`);
  const panel = $(`#${panelId}`);
  if (!handle || !panel) return;
  let dragging = false;
  let startX = 0;
  let startW = 0;
  handle.addEventListener('pointerdown', (e) => {
    dragging = true;
    startX = e.clientX;
    startW = panel.getBoundingClientRect().width;
    document.body.classList.add(cls);
    if (handle.setPointerCapture) handle.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  handle.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const width = Math.min(Math.max(min(), startW + grow * (e.clientX - startX)), max());
    apply(width);
  });
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    document.body.classList.remove(cls);
    const w = Math.round(panel.getBoundingClientRect().width);
    if (panelId === 'sidebar') document.documentElement.style.setProperty('--sidebar-w', `${w}px`);
    localStorage.setItem(storageKey, String(w));
  };
  handle.addEventListener('pointerup', stop);
  handle.addEventListener('pointercancel', stop);
}

/* ---- 侧边栏：宽度存 CSS 变量，折叠动画也跟着变 ---- */
function sidebarWidth() {
  const saved = Number(localStorage.getItem('lm-sidebar-width'));
  return Math.min(Math.max(220, saved || 274), sidebarMaxWidth());
}

function sidebarMaxWidth() {
  // 给对话区至少留 420px
  return Math.max(220, Math.min(480, window.innerWidth - 420));
}

function applySidebarWidth(width) {
  document.documentElement.style.setProperty('--sidebar-w', `${Math.round(width)}px`);
}

function bindSidebarResizer() {
  bindColResizer({
    handleId: 'sidebarResizer',
    panelId: 'sidebar',
    cls: 'sidebar-resizing',
    storageKey: 'lm-sidebar-width',
    grow: 1, // 面板在左侧：鼠标右移 = 变宽
    min: () => 220,
    max: sidebarMaxWidth,
    apply: applySidebarWidth,
  });
}

/* ---- 工作区面板（右侧文件树） ---- */
function wsWidth() {
  const saved = Number(localStorage.getItem('lm-ws-width'));
  const want = saved || 320;
  return Math.min(Math.max(260, want), wsMaxWidth());
}

function wsMaxWidth() {
  const app = $('#app');
  const sb = app.classList.contains('collapsed')
    ? 0
    : $('#sidebar').getBoundingClientRect().width || 274;
  const vp = $('#viewerPanel');
  const vw = vp && !vp.hidden ? vp.getBoundingClientRect().width || 0 : 0;
  // 给对话区至少留 380px
  return Math.max(260, window.innerWidth - sb - vw - 380);
}

function applyWsWidth(width) {
  const panel = $('#workspacePanel');
  if (!panel) return;
  panel.style.flexBasis = `${width}px`;
  panel.style.width = `${width}px`;
}

function bindWsResizer() {
  bindColResizer({
    handleId: 'wsResizer',
    panelId: 'workspacePanel',
    cls: 'ws-resizing',
    storageKey: 'lm-ws-width',
    grow: -1,
    min: () => 260,
    max: wsMaxWidth,
    apply: applyWsWidth,
  });
}

function splitCsvLine(line, delim) {
  const out = [];
  let cur = '';
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"' && line[i + 1] === '"') {
        cur += '"';
        i += 1;
      } else if (ch === '"') quoted = false;
      else cur += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === delim) {
      out.push(cur);
      cur = '';
    } else cur += ch;
  }
  out.push(cur);
  return out;
}

function csvTable(text) {
  const wrap = el('div', 'preview-table-wrap');
  const lines = String(text).split(/\r?\n/).filter((line) => line.length);
  const head = lines[0] || '';
  const delim = (head.match(/\t/g) || []).length > (head.match(/,/g) || []).length ? '\t' : ',';
  const table = el('table', 'preview-table');
  lines.slice(0, 500).forEach((line, idx) => {
    const tr = el('tr');
    splitCsvLine(line, delim).forEach((cell) => {
      const td = document.createElement(idx === 0 ? 'th' : 'td');
      td.textContent = cell;
      tr.appendChild(td);
    });
    table.appendChild(tr);
  });
  wrap.appendChild(table);
  if (lines.length > 500) {
    const note = el('div', 'preview-note inline');
    note.textContent = `共 ${lines.length} 行，仅显示前 500 行`;
    wrap.appendChild(note);
  }
  return wrap;
}

/* ---------------- MCP ---------------- */

async function loadMcp() {
  const data = await api('/api/mcp/servers');
  S.mcp = data.servers || [];
  $('#mcpPath').textContent = data.config_path || S.config?.paths?.mcp || '';
}

function renderMcpList() {
  const box = $('#mcpList');
  if (!box) return;
  box.innerHTML = '';
  if (!S.mcp.length) {
    box.innerHTML = '<div class="ws-empty">还没有接入 MCP 服务<br/>可以手动添加，也可以直接对模型说「帮我接入 xxx」</div>';
    return;
  }
  S.mcp.forEach((server) => {
    const row = el('div', 'mcp-row');
    const stateClass = server.state === 'ready' ? 'ready' : server.state === 'error' ? 'error' : '';
    const stateText = { ready: '已连接', error: '连接失败', connecting: '连接中', disconnected: '未连接' }[server.state] || server.state;
    const cmd = server.transport === 'http' ? server.url : [server.command, ...(server.args || [])].join(' ');
    const tools = (server.tools || []).map((t) => t.name).join(', ');
    row.innerHTML = `
      <div class="meta">
        <div class="name">${esc(server.name)}
          <span class="state-pill ${stateClass}">${stateText}</span>
          <span class="tag gray">${server.transport}</span>
          ${server.auth ? '<span class="tag gray">已配置鉴权</span>' : ''}
          ${server.enabled ? '' : '<span class="tag gray">已停用</span>'}
        </div>
        <div class="sub">${esc(cmd || '')}</div>
        <div class="sub">${server.tool_count} 个工具${tools ? '：' + esc(tools) : ''}${server.detail ? ' · ' + esc(server.detail) : ''}</div>
      </div>
      <div class="ops">
        <button data-act="debug">工具</button>
        <button data-act="connect">${server.state === 'ready' ? '重连' : '连接'}</button>
        <button data-act="edit">编辑</button>
        <button data-act="del" class="danger">删除</button>
      </div>`;
    row.querySelector('[data-act="debug"]').onclick = () => openMcpDebug(server.name);
    row.querySelector('[data-act="connect"]').onclick = async (e) => {
      e.currentTarget.disabled = true;
      e.currentTarget.textContent = '连接中…';
      try {
        await api(`/api/mcp/servers/${encodeURIComponent(server.name)}/connect`, { method: 'POST' });
        toast('已连接');
      } catch (err) {
        toast(`连接失败：${err.message}`, 3200);
      } finally {
        await Promise.all([loadMcp(), loadTools()]);
        renderMcpList();
        renderToolGrid();
      }
    };
    row.querySelector('[data-act="edit"]').onclick = () => openMcpEditor(server);
    row.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`删除 MCP 服务「${server.name}」？`)) return;
      await api(`/api/mcp/servers/${encodeURIComponent(server.name)}`, { method: 'DELETE' });
      await Promise.all([loadMcp(), loadTools()]);
      renderMcpList();
      renderToolGrid();
      toast('已删除');
    };
    box.appendChild(row);
  });
}

function openMcpEditor(server) {
  $('#mcpModalTitle').textContent = server ? '编辑 MCP 服务' : '添加 MCP 服务';
  $('#mcName').value = server?.name || '';
  $('#mcName').disabled = !!server;
  $('#mcTransport').value = server?.transport || 'stdio';
  $('#mcCommand').value = server?.command || '';
  $('#mcArgs').value = (server?.args || []).join('\n');
  $('#mcUrl').value = server?.url || '';
  $('#mcToken').value = server?.token || '';
  $('#mcHeaders').value = server?.headers && Object.keys(server.headers).length ? JSON.stringify(server.headers, null, 2) : '';
  $('#mcEnv').value = server?.env && Object.keys(server.env).length ? JSON.stringify(server.env, null, 2) : '';
  $('#mcDesc').value = server?.description || '';
  $('#mcEnabled').checked = server ? server.enabled !== false : true;
  toggleMcpFields();
  openModal('mcpModal');
}

function toggleMcpFields() {
  const transport = $('#mcTransport').value;
  const group = transport === 'stdio' ? 'stdio' : 'remote';
  $$('[data-mc]').forEach((node) => (node.hidden = node.dataset.mc !== group));
}

function readMcpForm() {
  const transport = $('#mcTransport').value;
  const body = {
    transport,
    command: $('#mcCommand').value.trim(),
    args: $('#mcArgs').value.split('\n').map((s) => s.trim()).filter(Boolean),
    url: $('#mcUrl').value.trim(),
    token: $('#mcToken').value.trim(),
    description: $('#mcDesc').value.trim(),
    enabled: $('#mcEnabled').checked,
    connect: true,
  };
  const envText = $('#mcEnv').value.trim();
  if (envText) {
    try {
      body.env = JSON.parse(envText);
    } catch (_) {
      throw new Error('环境变量需要是合法 JSON');
    }
  }
  const headerText = $('#mcHeaders').value.trim();
  if (headerText) {
    try {
      body.headers = JSON.parse(headerText);
    } catch (_) {
      throw new Error('请求头需要是合法 JSON');
    }
  }
  return body;
}

async function testMcpServer() {
  const btn = $('#testMcpBtn');
  let body;
  try {
    body = readMcpForm();
  } catch (err) {
    return toast(err.message, 2600);
  }
  btn.disabled = true;
  btn.textContent = '测试中…';
  try {
    const data = await api('/api/mcp/test', { method: 'POST', body });
    if (data.ok) {
      toast(`连接成功，发现 ${data.tool_count} 个工具（${data.duration_ms}ms）`, 3200);
      S.mcpProbe = data.tools || [];
    } else {
      toast(`连接失败：${data.error}`, 5200);
    }
  } catch (err) {
    toast(`测试失败：${err.message}`, 4200);
  } finally {
    btn.disabled = false;
    btn.textContent = '测试连接';
  }
}

async function saveMcpServer() {
  const name = $('#mcName').value.trim();
  if (!name) return toast('请填写服务名');
  let body;
  try {
    body = { ...readMcpForm(), name, connect: true };
  } catch (err) {
    return toast(err.message, 2600);
  }
  try {
    await api('/api/mcp/servers', { method: 'POST', body });
    closeModal('mcpModal');
    await Promise.all([loadMcp(), loadTools()]);
    renderMcpList();
    renderToolGrid();
    toast('已保存并连接');
  } catch (err) {
    toast(`保存失败：${err.message}`, 4200);
  }
}

/* ---------------- MCP 工具调试 ---------------- */

async function openMcpDebug(name) {
  if (!S.mcp.length) await loadMcp();
  if (!S.mcp.length) return toast('还没有配置 MCP 服务', 2800);
  const sel = $('#dbgServer');
  sel.innerHTML = S.mcp
    .map((s) => `<option value="${esc(s.name)}">${esc(s.name)}（${esc(s.transport)}）</option>`)
    .join('');
  sel.value = name || S.mcp[0].name;
  resetDbgPanel();
  openModal('mcpDebugModal');
  await loadDbgTools(false);
}

function resetDbgPanel() {
  S.dbg = { server: $('#dbgServer').value, tools: [], tool: null };
  $('#dbgToolName').textContent = '选择左侧的工具';
  $('#dbgToolDesc').textContent = '';
  $('#dbgSchema').textContent = '-';
  $('#dbgArgs').value = '{}';
  $('#dbgResult').textContent = '还没有调用';
  $('#dbgResult').classList.remove('err');
  $('#dbgStatus').textContent = '';
  $('#dbgStatus').className = 'dbg-status';
  $('#dbgCall').disabled = true;
  $('#dbgState').textContent = '';
  $('#dbgToolList').innerHTML = '';
}

async function loadDbgTools(refresh) {
  const server = $('#dbgServer').value;
  if (!server) return;
  S.dbg = S.dbg || { tools: [], tool: null };
  S.dbg.server = server;
  S.dbg.tool = null;
  $('#dbgCall').disabled = true;
  $('#dbgState').textContent = refresh ? '正在重新连接…' : '正在读取工具列表…';
  $('#dbgToolList').innerHTML = '<div class="dbg-tool-item-empty">加载中…</div>';
  try {
    const data = await api(
      `/api/mcp/servers/${encodeURIComponent(server)}/tools?refresh=${refresh ? 'true' : 'false'}`
    );
    S.dbg.tools = data.tools || [];
    const stateText = { ready: '已连接', error: '连接失败', disconnected: '未连接', connecting: '连接中' }[data.state] || data.state;
    $('#dbgState').textContent = `${stateText} · ${S.dbg.tools.length} 个工具${data.error ? ' · ' + data.error : ''}`;
    renderDbgTools();
  } catch (err) {
    $('#dbgState').textContent = `读取失败：${err.message}`;
    $('#dbgToolList').innerHTML = '<div class="dbg-tool-item-empty">读取失败，试试右上角「重新连接」</div>';
  }
}

function renderDbgTools() {
  const box = $('#dbgToolList');
  box.innerHTML = '';
  const tools = S.dbg?.tools || [];
  if (!tools.length) {
    box.innerHTML = '<div class="dbg-tool-item-empty">这个服务没有暴露工具</div>';
    return;
  }
  tools.forEach((tool) => {
    const btn = el('button', 'dbg-tool');
    btn.innerHTML = `${esc(tool.name)}<small>${esc(tool.description || '')}</small>`;
    if (S.dbg.tool?.name === tool.name) btn.classList.add('active');
    btn.onclick = () => selectDbgTool(tool);
    box.appendChild(btn);
  });
}

function sampleArgs(schema) {
  const props = (schema && schema.properties) || {};
  const out = {};
  Object.entries(props).forEach(([key, def]) => {
    if (def.default !== undefined) out[key] = def.default;
    else if (Array.isArray(def.enum) && def.enum.length) out[key] = def.enum[0];
    else if (def.type === 'number' || def.type === 'integer') out[key] = 0;
    else if (def.type === 'boolean') out[key] = false;
    else if (def.type === 'array') out[key] = [];
    else if (def.type === 'object') out[key] = {};
    else out[key] = '';
  });
  return out;
}

function selectDbgTool(tool) {
  S.dbg.tool = tool;
  renderDbgTools();
  $('#dbgToolName').textContent = tool.name;
  $('#dbgToolDesc').textContent = tool.description || '';
  $('#dbgSchema').textContent = JSON.stringify(tool.parameters || {}, null, 2);
  $('#dbgArgs').value = JSON.stringify(sampleArgs(tool.parameters), null, 2);
  $('#dbgCall').disabled = false;
  $('#dbgResult').textContent = '还没有调用';
  $('#dbgResult').classList.remove('err');
  $('#dbgStatus').textContent = '';
  $('#dbgStatus').className = 'dbg-status';
}

async function callDbgTool() {
  const tool = S.dbg?.tool;
  if (!tool) return;
  let args;
  try {
    args = JSON.parse($('#dbgArgs').value || '{}');
  } catch (err) {
    return toast(`参数不是合法 JSON：${err.message}`, 3200);
  }
  const btn = $('#dbgCall');
  btn.disabled = true;
  btn.textContent = '调用中…';
  $('#dbgStatus').textContent = '请求中…';
  $('#dbgStatus').className = 'dbg-status';
  try {
    const data = await api(`/api/mcp/servers/${encodeURIComponent(S.dbg.server)}/call`, {
      method: 'POST',
      body: { tool: tool.name, arguments: args },
    });
    const extra = data.structured ? `\n\n--- structured ---\n${JSON.stringify(data.structured, null, 2)}` : '';
    $('#dbgResult').textContent = (data.content || '（无返回内容）') + extra;
    $('#dbgResult').classList.toggle('err', !data.ok);
    $('#dbgStatus').textContent = `${data.ok ? '成功' : '返回错误'} · ${data.duration_ms}ms`;
    $('#dbgStatus').className = `dbg-status ${data.ok ? 'ok' : 'err'}`;
  } catch (err) {
    $('#dbgResult').textContent = err.message;
    $('#dbgResult').classList.add('err');
    $('#dbgStatus').textContent = '请求失败';
    $('#dbgStatus').className = 'dbg-status err';
  } finally {
    btn.disabled = false;
    btn.textContent = '调用';
  }
}

/* ---------------- 技能设置 ---------------- */

function renderSkillList() {
  const box = $('#skillList');
  if (!box) return;
  box.innerHTML = '';
  if (!S.skills.length) {
    box.innerHTML = '<div class="ws-empty">还没有技能<br/>点右上角新建，或让模型用 skill_create 生成</div>';
    return;
  }
  S.skills.forEach((skill) => {
    const row = el('div', 'skill-row');
    const tags = (skill.tags || []).map((t) => `<span class="tag gray">${esc(t)}</span>`).join(' ');
    row.innerHTML = `
      <div class="meta">
        <div class="name">${esc(skill.name || skill.slug)}
          <span class="tag">${esc(skill.slug)}</span>
          ${skill.enabled ? '' : '<span class="tag gray">已停用</span>'}
          ${tags}
        </div>
        <div class="sub">${esc(skill.description || '')}</div>
      </div>
      <div class="ops">
        <button data-act="toggle">${skill.enabled ? '停用' : '启用'}</button>
        <button data-act="use">本轮使用</button>
        <button data-act="edit">编辑</button>
        <button data-act="del" class="danger">删除</button>
      </div>`;
    row.querySelector('[data-act="toggle"]').onclick = async () => {
      await api(`/api/skills/${skill.slug}/toggle`, {
        method: 'POST',
        body: { enabled: !skill.enabled },
      });
      await loadSkills();
      renderSkillList();
      renderSkillMenu();
    };
    row.querySelector('[data-act="use"]').onclick = async () => {
      await toggleSkill(skill.slug, true);
      closeModal('settingsModal');
      toast(`已启用技能：${skill.name || skill.slug}`);
    };
    row.querySelector('[data-act="edit"]').onclick = () => openSkillEditor(skill);
    row.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm(`删除技能「${skill.name || skill.slug}」？`)) return;
      await api(`/api/skills/${skill.slug}`, { method: 'DELETE' });
      await loadSkills();
      renderSkillList();
      renderSkillMenu();
      toast('已删除');
    };
    box.appendChild(row);
  });
}

async function openSkillEditor(skill) {
  S.editingSkill = skill?.slug || null;
  $('#skillModalTitle').textContent = skill ? '编辑技能' : '新建技能';
  $('#skName').value = skill?.slug || '';
  $('#skName').disabled = !!skill;
  $('#skTitle').value = skill?.name || '';
  if (skill) {
    const detail = await api(`/api/skills/${skill.slug}`);
    $('#skContent').value = detail.raw || detail.body || '';
  } else {
    $('#skContent').value = '---\nname: 新技能\ndescription: 一句话说明这个技能解决什么问题\n---\n\n# 适用场景\n\n# 执行步骤\n\n1. \n\n# 输出格式\n';
  }
  openModal('skillModal');
}

async function saveSkillEditor() {
  const slug = S.editingSkill || $('#skName').value.trim().toLowerCase();
  if (!slug) return toast('请填写技能标识');
  let content = $('#skContent').value;
  const title = $('#skTitle').value.trim();
  if (title && !content.trimStart().startsWith('---')) {
    content = `---\nname: ${title}\n---\n\n${content}`;
  }
  try {
    if (S.editingSkill) {
      await api(`/api/skills/${slug}`, { method: 'PUT', body: { content } });
    } else {
      await api('/api/skills', { method: 'POST', body: { name: slug, content } });
    }
    closeModal('skillModal');
    await loadSkills();
    renderSkillList();
    renderSkillMenu();
    toast('技能已保存');
  } catch (err) {
    toast(`保存失败：${err.message}`, 3200);
  }
}

/* ---------------- 记忆 ---------------- */

async function loadMemory() {
  try {
    S.memory = await api('/api/memory');
  } catch (_) {
    S.memory = { text: '', path: '' };
  }
}

function renderMemory() {
  $('#memoryText').value = S.memory?.text || '';
  $('#memoryPath').textContent = S.memory?.path || S.config?.paths?.memory || '';
}

async function saveMemory() {
  const data = await api('/api/memory', { method: 'PUT', body: { text: $('#memoryText').value } });
  S.memory = data;
  toast('记忆已保存');
}

/* ---------------- 上下文压缩 ---------------- */

async function compactNow(convId) {
  const id = convId || S.conv?.id;
  if (!id) return toast('先选择会话');
  toast('正在压缩…', 4000);
  try {
    const result = await api(`/api/conversations/${id}/compact`, { method: 'POST' });
    if (!result.ok) return toast(result.detail || '压缩未执行', 3000);
    toast(`已压缩 ${result.covered_messages} 条消息`);
    await refreshAfterTurn();
    if (S.contextInfo) $('#convContext').innerHTML = '';
  } catch (err) {
    toast(`压缩失败：${err.message}`, 3200);
  }
}

/* ---------------- 导出 ---------------- */

function exportConversation() {
  if (!S.conv) return toast('还没有可导出的对话');
  window.open(`/api/conversations/${S.conv.id}/export`, '_blank');
}

/* 启动 */
bind();
boot().catch((err) => {
  console.error(err);
  toast(`初始化失败：${err.message}`, 4000);
});
