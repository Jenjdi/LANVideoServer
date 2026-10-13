'use strict';
const el = id => document.getElementById(id);
const player = el('player');
let user = null, csrf = '', offset = 0, total = 0, selected = null, grantId = null, resetId = null;
let uploadRequest = null, uploadLimit = 0, lastSaved = 0, historyQueue = Promise.resolve();
let uploadCancelled = false;
let btRetryTimer = null, btRetryCount = 0, btReloading = false, btWantsPlay = false, btFilesTask = null, btCompleteFailures = 0, btLastReloadComplete = false;
let authVersion = 0, libraryVersion = 0;
let nextAudit = null;
const PAGE_SIZE = 24;
function message(text) { el('message').textContent = text; }
function size(bytes) { return bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1048576).toFixed(1)} MiB`; }
function time(seconds) { if (seconds === null || !Number.isFinite(seconds)) return '时长未知'; return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`; }
async function api(path, options = {}) {
  const requestedVersion = authVersion;
  const headers = { ...options.headers };
  if (options.body !== undefined) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(options.body); }
  if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrf;
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login' && requestedVersion === authVersion) signedOut();
    const error = new Error(result.error || `请求失败（${response.status}）`); error.status = response.status; throw error;
  }
  return result;
}
function onForm(id, action) { el(id).addEventListener('submit', async event => { event.preventDefault(); message(''); try { await action(); } catch (error) { message(error.message); } }); }
function button(label, action, dangerous = false) {
  const node = document.createElement('button'); node.type = 'button'; node.textContent = label;
  if (dangerous) node.className = 'danger';
  node.addEventListener('click', async () => { message(''); try { await action(); } catch (error) { message(error.message); } });
  return node;
}
function stopPlayer() { clearTimeout(btRetryTimer); btRetryTimer = null; btRetryCount = 0; btCompleteFailures = 0; btLastReloadComplete = false; btReloading = false; btWantsPlay = false; selected = null; el('retry-bt-playback').hidden = true; player.pause(); player.removeAttribute('src'); player.load(); el('player-panel').hidden = true; }
function signedOut() {
  authVersion++; libraryVersion++; historyQueue = Promise.resolve();
  user = null; csrf = ''; stopPlayer();
  if (uploadRequest) uploadRequest.abort();
  el('account').hidden = true; el('workspace').hidden = true; el('login-section').hidden = false;
  el('media-list').replaceChildren(); el('user-list').replaceChildren(); el('grant-users').replaceChildren();
  el('total').textContent = ''; el('page-number').textContent = ''; el('root-form').reset();
  el('create-user-form').reset(); el('password-form').reset(); el('grant-panel').hidden = true;
  btFilesTask = null; el('bt-files-panel').hidden = true; el('bt-file-list').replaceChildren();
  el('reset-dialog').close(); el('reset-form').reset();
  el('share-url').value = ''; el('share-list').replaceChildren(); el('remote-test-result').textContent = '';
  el('monitor-summary').replaceChildren(); el('audit-list').replaceChildren(); nextAudit = null; el('older-audit').hidden = true;
}
function tab(section) {
  for (const name of ['library', 'administration', 'password-section', 'tasks-section']) el(name).hidden = name !== section;
  el('remote-administration').hidden = section !== 'administration' || user?.role !== 'admin';
  el('media-tab').classList.toggle('active', section === 'library');
  el('admin-tab').classList.toggle('active', section === 'administration');
  el('tasks-tab').classList.toggle('active', section === 'tasks-section');
  el('password-tab').classList.toggle('active', section === 'password-section');
}
async function signedIn(identity) {
  authVersion++; el('total').textContent = '';
  user = identity; el('identity').textContent = `${user.username} · ${user.role === 'admin' ? '管理员' : '普通用户'}`;
  el('account').hidden = false; el('workspace').hidden = false; el('login-section').hidden = true;
  el('admin-tab').hidden = user.role !== 'admin'; offset = 0; tab('library'); await library();
}
async function library() {
  const requestedUser = user, requestedVersion = ++libraryVersion;
  const query = new URLSearchParams({ q: el('search').value, format: el('format').value, limit: PAGE_SIZE, offset });
  const result = await api(`/api/media?${query}`);
  if (user !== requestedUser || libraryVersion !== requestedVersion) return;
  total = result.total;
  el('total').textContent = `${total} 个可访问视频`; el('empty').hidden = result.media.length !== 0;
  el('media-list').replaceChildren();
  for (const item of result.media) {
    const card = document.createElement('article'); card.className = 'card';
    const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = item.format.toUpperCase();
    const heading = document.createElement('h3'); heading.textContent = item.name;
    const details = document.createElement('p'); details.className = 'details'; details.textContent = `${size(item.size)} · ${time(item.duration)}`;
    const position = document.createElement('p'); position.className = 'details'; position.textContent = item.position > 0 ? `上次播放到 ${time(item.position)}` : (item.metadata_state === 'invalid' ? '媒体探测失败，浏览器可能无法播放' : '尚无播放记录');
    const actions = document.createElement('div'); actions.className = 'actions'; actions.append(button('播放', () => play(item)));
    if (user.role === 'admin') actions.append(button('授权', () => grants(item)));
    if (user.role === 'admin') actions.append(button('分享', async () => { await administration(); tab('administration'); el('share-media-id').value = item.id; el('create-share-form').scrollIntoView(); }));
    card.append(badge, heading, details, position, actions); el('media-list').append(card);
  }
  el('previous').disabled = offset === 0; el('next').disabled = offset + PAGE_SIZE >= total;
  el('page-number').textContent = `${Math.floor(offset / PAGE_SIZE) + 1} / ${Math.max(1, Math.ceil(total / PAGE_SIZE))}`;
}
async function saveHistory(force = false, keepalive = false) {
  if (!selected || !user || player.readyState < 1 || !Number.isFinite(player.currentTime) || (selected.bt && (btReloading || player.error))) return;
  if (!force && Date.now() - lastSaved < 5000) return;
  lastSaved = Date.now(); const id = selected.id, position = player.currentTime, requestedUser = user, requestedVersion = authVersion;
  const bt = selected.bt; if (bt) selected.position = position;
  historyQueue = historyQueue.catch(() => {}).then(() => {
    if (user !== requestedUser || authVersion !== requestedVersion) return;
    return api(bt ? `/api/tasks/${bt.task}/files/${bt.index}/history` : '/api/history', { method: 'POST', body: bt ? { position } : { media_id: id, position }, keepalive });
  });
  try { await historyQueue; } catch (error) { el('player-error').textContent = `播放记录保存失败：${error.message}`; }
}
async function play(item) {
  await saveHistory(true); stopPlayer(); selected = item; lastSaved = Date.now();
  el('player-title').textContent = item.name; el('player-error').textContent = ''; el('player-panel').hidden = false;
  el('retry-bt-playback').hidden = !item.bt; btLastReloadComplete = !!item.complete;
  player.src = item.bt ? `/bt/${item.bt.task}/file/${item.bt.index}` : `/media/${item.id}`; player.load();
  el('player-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
player.addEventListener('loadedmetadata', () => {
  if (!selected) return;
  btReloading = false;
  el('playback-status').textContent = `实际媒体时长 ${time(player.duration)}。播放位置每 5 秒及暂停/定位时保存。`;
  if (selected.position > 0 && selected.position < player.duration - 0.5) player.currentTime = selected.position;
  if (selected.bt) { el('player-error').textContent = ''; el('playback-status').textContent += ' BT 响应只包含再次校验通过的分片，缺少数据时会等待或重试。'; if (btWantsPlay) player.play().catch(() => {}); }
});
player.addEventListener('timeupdate', () => { if (selected?.bt && !btReloading && !player.error && player.readyState>=1 && Number.isFinite(player.currentTime)) selected.position = player.currentTime; saveHistory(); });
player.addEventListener('seeking', () => { if (selected?.bt && Number.isFinite(player.currentTime)) selected.position = player.currentTime; });
for (const event of ['pause', 'seeked', 'ended']) player.addEventListener(event, () => { saveHistory(true); });
player.addEventListener('error', async () => {
  if (!selected) return;
  if (selected.bt) { retryBtPlayback(); return; }
  const id = selected.id;
  try {
    const response = await fetch(`/media/${id}`, { method: 'HEAD', credentials: 'same-origin' });
    el('player-error').textContent = response.ok ? '浏览器无法解码此视频，或文件已损坏。请使用兼容的 MP4/WebM 文件。' : `视频读取失败（${response.status}）：请检查登录状态、授权、文件是否存在或是否需要重新扫描。`;
  } catch { el('player-error').textContent = '无法连接视频服务，请检查网络连接后重试。'; }
});
window.addEventListener('pagehide', () => { saveHistory(true, true); });
onForm('login-form', async () => {
  const password = el('password').value; el('password').value = '';
  const result = await api('/api/login', { method: 'POST', body: { username: el('username').value, password } });
  csrf = result.csrf_token; await signedIn(result.user);
});
el('logout').addEventListener('click', async () => { try { await saveHistory(true); await api('/api/logout', { method: 'POST' }); signedOut(); } catch (error) { message(error.message); } });
el('media-tab').addEventListener('click', async () => { tab('library'); try { await library(); } catch (error) { message(error.message); } });
el('admin-tab').addEventListener('click', async () => { try { await administration(); tab('administration'); } catch (error) { message(error.message); } });
el('password-tab').addEventListener('click', () => { tab('password-section'); });
onForm('search-form', async () => { offset = 0; await library(); });
el('previous').addEventListener('click', async () => { offset = Math.max(0, offset - PAGE_SIZE); try { await library(); } catch (error) { message(error.message); } });
el('next').addEventListener('click', async () => { offset += PAGE_SIZE; try { await library(); } catch (error) { message(error.message); } });
el('close-player').addEventListener('click', async () => { await saveHistory(true); stopPlayer(); await library(); });
async function administration() {
  const requestedUser = user;
  const config = await api('/api/media/config');
  if (user !== requestedUser) return;
  el('root-path').value = config.root; uploadLimit = config.upload_limit_bytes;
  el('upload-limit').textContent = `单文件上限 ${size(uploadLimit)}；${config.ffprobe_available ? '已启用视频流校验与时长探测' : 'FFprobe 不可用，仅检查扩展名及基础容器签名'}`;
  await users();
  await refreshRemote(); await refreshShares();
  await refreshMonitor();
}
onForm('root-form', async () => {
  if (!window.confirm('更换共享目录会清除旧目录的索引、授权和播放记录。继续？')) return;
  await api('/api/media/root', { method: 'POST', body: { path: el('root-path').value } });
  stopPlayer(); el('scan-status').textContent = '目录已更新，请扫描后重新授权。'; await administration();
});
el('scan').addEventListener('click', async () => {
  el('scan').disabled = true; el('scan-status').textContent = '正在扫描真实文件并探测媒体信息…';
  try {
    const result = await api('/api/media/scan', { method: 'POST', body: {} });
    el('scan-status').textContent = `索引 ${result.indexed} 个视频，更新 ${result.changed} 个，移除 ${result.removed} 个，跳过 ${result.skipped} 项；${result.metadata_unavailable} 个时长信息不可用。`;
  } catch (error) { el('scan-status').textContent = error.message; }
  finally { el('scan').disabled = false; }
});
onForm('upload-form', async () => {
  const file = el('upload-file').files[0]; if (!file) throw new Error('请选择视频文件。');
  if (file.size > uploadLimit) throw new Error('文件超过服务器上传上限。');
  el('upload-button').disabled = true; el('cancel-upload').hidden = false;
  el('upload-progress').hidden = false; el('upload-progress').value = 0;
  el('upload-status').textContent = '开始上传…'; uploadCancelled = false;
  const uploadVersion = authVersion;
  el('cancel-upload').textContent = el('resumable-upload').checked ? '暂停上传' : '取消上传';
  try {
    const result = el('resumable-upload').checked ? await resumableUpload(file, uploadVersion) : await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); uploadRequest = xhr;
      xhr.open('POST', `/api/media/upload?filename=${encodeURIComponent(file.name)}`); xhr.timeout = 130000;
      xhr.setRequestHeader('Content-Type', 'application/octet-stream'); xhr.setRequestHeader('X-CSRF-Token', csrf);
      xhr.upload.addEventListener('progress', event => {
        if (event.lengthComputable) el('upload-progress').value = event.loaded / event.total * 100;
        el('upload-status').textContent = `已发送 ${size(event.loaded)}${event.lengthComputable ? ` / ${size(event.total)}` : ''}，等待服务器校验与保存。`;
      });
      xhr.onload = () => {
        try { const value = JSON.parse(xhr.responseText); if (xhr.status === 201) resolve(value); else { if (xhr.status === 401) signedOut(); reject(new Error(value.error || `上传失败（${xhr.status}）`)); } }
        catch { reject(new Error('服务器未返回有效上传结果。')); }
      };
      xhr.onerror = () => reject(new Error('上传连接中断，请检查网络后重试。'));
      xhr.ontimeout = () => reject(new Error('上传超时，未收到保存成功确认。'));
      xhr.onabort = () => reject(new Error('已取消传输。若服务端已经保存完整文件，请刷新视频库确认；取消传输不会撤销已完成的保存。'));
      xhr.send(file);
    });
    if (authVersion !== uploadVersion) return;
    el('upload-progress').value = 100; el('upload-status').textContent = `上传成功：${result.media.name}（${size(result.media.size)}），已加入视频库。`;
    el('upload-form').reset();
  } catch (error) { el('upload-status').textContent = error.name === 'AbortError' ? '已暂停传输，重新选择相同文件可从确认的断点继续。' : (error instanceof TypeError && el('resumable-upload').checked ? '分块连接中断，已确认的断点保留；检查网络后重新选择相同文件继续。' : error.message); }
  finally { uploadRequest = null; el('upload-button').disabled = false; el('cancel-upload').hidden = true; }
});
el('cancel-upload').addEventListener('click', () => { uploadCancelled = true; if (uploadRequest) uploadRequest.abort(); });

async function resumableUpload(file, version) {
  const verifyAccount = () => {
    if (uploadCancelled || version !== authVersion) throw new Error('已暂停传输，已确认分块保留 24 小时；重新选择相同文件可继续。已完成的保存不会被撤销。');
  };
  const accountId = user.id, chunkSize = 1048576, hashes = [], entire = sha256.create();
  const oldRequest = uploadRequest; uploadRequest = { abort() { uploadCancelled = true; } };
  for (let at = 0; at < file.size; at += chunkSize) {
    verifyAccount(); const chunk = new Uint8Array(await file.slice(at, at + chunkSize).arrayBuffer());
    hashes.push(sha256(chunk)); entire.update(chunk);
    el('upload-status').textContent = `正在计算文件校验值 ${size(Math.min(at + chunkSize, file.size))} / ${size(file.size)}；尚未传输。`;
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  verifyAccount();
  const fingerprint = sha256(hashes.join(''));
  const key = `lv-upload:${accountId}:${file.name}:${file.size}:${fingerprint}`;
  let state = null, cached = null;
  try { cached = localStorage.getItem(key); } catch { /* Storage denied: current-page resume still works. */ }
  if (cached) {
    try { state = await api(`/api/uploads/${cached}`); }
    catch (error) {
      if ([404, 410].includes(error.status)) { try { localStorage.removeItem(key); } catch {} }
      else throw new Error(`原上传任务不可继续：${error.message}。请放弃断点后重试，断点编号：${cached}`);
    }
  }
  verifyAccount();
  if (!state) {
    state = await api('/api/uploads', { method: 'POST', body: { filename: file.name, size: file.size, hashes } });
    try { localStorage.setItem(key, state.id); } catch { /* No credentials are stored. */ }
  }
  if (state.fingerprint !== fingerprint || state.size !== file.size) throw new Error('所选文件与原上传的校验清单不一致。');
  while (state.offset < file.size) {
    verifyAccount(); const at = state.offset;
    const data = file.slice(at, at + chunkSize);
    uploadRequest = new AbortController();
    const response = await fetch(`/api/uploads/${state.id}/chunk`, { method: 'PUT', credentials: 'same-origin', signal: uploadRequest.signal,
      headers: { 'Content-Type': 'application/octet-stream', 'X-CSRF-Token': csrf, 'Upload-Offset': String(at) }, body: data });
    verifyAccount();
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '分块传输失败，请重新上传以查询服务器断点。');
    state = result; el('upload-progress').value = state.offset / state.size * 100;
    el('upload-status').textContent = `服务器已校验并保存 ${size(state.offset)} / ${size(state.size)}，断点编号 ${state.id}。`;
  }
  verifyAccount(); uploadRequest = { abort() { uploadCancelled = true; } };
  el('upload-status').textContent = '分块已保存，正在校验完整文件并发布…';
  const result = await api(`/api/uploads/${state.id}/complete`, { method: 'POST', body: {} });
  verifyAccount();
  if (result.sha256 !== entire.hex()) throw new Error('服务器完整文件 SHA-256 与本地文件不一致，请联系管理员。');
  try { localStorage.removeItem(key); } catch { /* No persistent storage available. */ }
  uploadRequest = oldRequest; return result;
}
onForm('discard-upload-form', async () => {
  const id = el('discard-upload-id').value.trim();
  if (!/^[0-9a-f]{32}$/.test(id)) throw new Error('请输入 32 位断点编号。');
  await api(`/api/uploads/${id}`, { method: 'DELETE' });
  for (let i = localStorage.length - 1; i >= 0; i--) {
    const key = localStorage.key(i);
    if (key.startsWith(`lv-upload:${user.id}:`) && localStorage.getItem(key) === id) localStorage.removeItem(key);
  }
  el('upload-status').textContent = '断点已放弃；已发布的文件保留。';
});

async function users() {
  const requestedUser = user;
  const result = await api('/api/users');
  if (user !== requestedUser) return;
  el('user-list').replaceChildren();
  for (const item of result.users) {
    const row = document.createElement('div'); row.className = 'user-row'; row.dataset.userId = item.id;
    const name = document.createElement('span'); name.className = 'user-name'; name.textContent = `${item.username} (#${item.id}) · ${item.disabled ? '已禁用' : '启用'}`;
    const role = document.createElement('select'); role.setAttribute('aria-label', `${item.username} 的角色`);
    for (const value of ['user', 'admin']) { const option = document.createElement('option'); option.value = value; option.textContent = value === 'admin' ? '管理员' : '普通用户'; role.append(option); }
    role.value = item.role;
    role.addEventListener('change', async () => { try { await api(`/api/users/${item.id}`, { method: 'PATCH', body: { role: role.value } }); if (item.id === user.id) signedOut(); else await users(); } catch (error) { role.value = item.role; message(error.message); } });
    row.append(name, role, button(item.disabled ? '启用' : '禁用', async () => { await api(`/api/users/${item.id}`, { method: 'PATCH', body: { disabled: !item.disabled } }); if (item.id === user.id) signedOut(); else await users(); }), button('重置密码', () => { resetId = item.id; el('reset-form').reset(); el('reset-dialog').showModal(); }), button('删除', async () => { if (window.confirm(`删除用户 ${item.username} 并撤销其会话？`)) { await api(`/api/users/${item.id}`, { method: 'DELETE' }); if (item.id === user.id) signedOut(); else await users(); } }, true));
    el('user-list').append(row);
  }
}
onForm('create-user-form', async () => {
  const password = el('new-password').value; el('new-password').value = '';
  await api('/api/users', { method: 'POST', body: { username: el('new-username').value, password, role: el('new-role').value } }); el('create-user-form').reset(); await users();
});
async function grants(item) {
  await administration(); tab('administration'); grantId = item.id;
  const [permissions, accounts] = await Promise.all([api(`/api/media/${item.id}/grants`), api('/api/users')]);
  el('grant-title').textContent = `授权：${item.name}`; el('grant-users').replaceChildren();
  for (const account of accounts.users.filter(item => item.role === 'user' && !item.disabled)) {
    const label = document.createElement('label'); label.className = 'grant-option';
    const input = document.createElement('input'); input.type = 'checkbox'; input.value = account.id; input.checked = permissions.user_ids.includes(account.id);
    const name = document.createElement('span'); name.textContent = account.username; label.append(input, name); el('grant-users').append(label);
  }
  if (!el('grant-users').childElementCount) el('grant-users').textContent = '尚无启用的普通用户，请先创建。';
  el('grant-panel').hidden = false; el('grant-panel').scrollIntoView({ block: 'center' });
}
onForm('grants-form', async () => { const user_ids = [...el('grant-users').querySelectorAll('input:checked')].map(input => Number(input.value)); await api(`/api/media/${grantId}/grants`, { method: 'POST', body: { user_ids } }); el('grant-panel').hidden = true; message('视频授权已保存。'); });
el('cancel-grants').addEventListener('click', () => { el('grant-panel').hidden = true; });
onForm('reset-form', async () => {
  const password = el('reset-password').value; if (password !== el('reset-confirm').value) throw new Error('两次密码不一致。');
  await api(`/api/users/${resetId}/password`, { method: 'POST', body: { password } }); el('reset-dialog').close(); el('reset-form').reset(); if (resetId === user.id) signedOut(); else message('密码已重置，目标用户需要重新登录。');
});
el('reset-cancel').addEventListener('click', () => { el('reset-dialog').close(); el('reset-form').reset(); });
onForm('password-form', async () => {
  const password = el('replacement-password').value; if (password !== el('confirm-password').value) throw new Error('两次密码不一致。');
  await saveHistory(true); await api('/api/password', { method: 'POST', body: { old_password: el('old-password').value, new_password: password } }); el('password-form').reset(); signedOut(); message('密码已修改，请重新登录。');
});
(async () => {
  el('transport').textContent = location.protocol === 'https:' ? '当前浏览器通过 HTTPS 连接；反向代理与公网部署状态需单独验证。' : '当前为 HTTP 开发连接。公网访问需要部署 HTTPS 入口。';
  try { const status = await api('/api/bootstrap/status'); el('bootstrap-note').hidden = status.initialized; } catch (error) { message(error.message); }
  try { const identity = await api('/api/me'); csrf = identity.csrf_token; await signedIn(identity.user); }
  catch (error) { if (error.message !== 'authentication required' && error.message !== 'session invalid or expired') message(error.message); }
})();

async function tasks() {
  const version = authVersion;
  const result = await api('/api/tasks'); if (version !== authVersion) return;
  el('task-list').replaceChildren();
  for (const item of result.tasks) {
    const row = document.createElement('section'); row.className = 'panel'; row.dataset.taskId = item.id;
    const title = document.createElement('h3'); title.textContent = `${item.name} (#${item.id})`;
    const state = document.createElement('p');
    state.textContent = item.state === 'unavailable' ? '恢复失败，请检查服务端目录与元数据。' : `${item.paused ? '暂停' : item.state} · 已校验 ${(item.progress * 100).toFixed(1)}% · ${size(item.verified_bytes)} / ${size(item.total_bytes)} · ${size(item.download_bytes_per_second)}/s · ${item.peers} 个连接 Peer · ${item.verified_pieces}/${item.piece_count} 个分片`;
    const bar = document.createElement('progress'); bar.max = 1; bar.value = item.progress || 0;
    row.append(title, state, bar);
    if (item.error) { const error = document.createElement('p'); error.textContent = item.error.message; row.append(error); }
    for (const [action, label] of [['pause', '暂停'], ['resume', '恢复'], ['recheck', '重新校验']]) row.append(button(label, async () => { await api(`/api/tasks/${item.id}/${action}`, { method: 'POST', body: {} }); await tasks(); }));
    row.append(button('视频文件', () => btFiles(item.id)));
    row.append(button('移除任务', async () => {
      if (!window.confirm('移除任务后已下载文件仍保留在服务器任务目录中。继续？')) return;
      await api(`/api/tasks/${item.id}`, { method: 'DELETE' }); await tasks();
    }, true)); el('task-list').append(row);
  }
  el('task-status').textContent = `共 ${result.tasks.length} 个可访问任务；数据由 libtorrent 提供。每 2 秒更新可见页面。`;
}
async function btConfig() {
  const version = authVersion, result = await api('/api/bt/config'); if (version !== authVersion) return;
  el('download-root').value = result.download_root;
  el('proxy-type').value = result.proxy.type; el('proxy-host').value = result.proxy.host;
  el('proxy-port').value = result.proxy.port || ''; el('proxy-timeout').value = result.proxy.timeout_ms;
  el('proxy-user').value = ''; el('proxy-password').value = '';
  el('proxy-summary').textContent = `当前 ${result.proxy.type}；${result.proxy.credentials_configured ? '已加密保存凭据。再次保存时需重新输入凭据。' : '未配置凭据。'}`;
}
el('tasks-tab').addEventListener('click', async () => {
  tab('tasks-section'); el('bt-admin').hidden = user.role !== 'admin';
  try { await tasks(); if (user.role === 'admin') await btConfig(); } catch (error) { message(error.message); }
});
el('refresh-tasks').addEventListener('click', () => tasks().catch(error => message(error.message)));
setInterval(() => { if (user && !el('tasks-section').hidden) tasks().catch(error => message(error.message)); }, 2000);
onForm('torrent-form', async () => {
  const file = el('torrent-file').files[0]; if (!file || file.size > 1048576) throw new Error('请选择不超过 1 MiB 的 v1 .torrent。');
  const response = await fetch('/api/torrents', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/x-bittorrent', 'X-CSRF-Token': csrf }, body: file });
  const result = await response.json(); if (!response.ok) throw new Error(result.error);
  el('torrent-form').reset(); message('真实种子已导入，请点击恢复开始下载。'); await tasks();
});
onForm('download-root-form', async () => { await api('/api/bt/root', { method: 'POST', body: { path: el('download-root').value } }); message('下载目录已更新，对新任务生效。'); });
onForm('proxy-form', async () => {
  const settings = { type: el('proxy-type').value, host: el('proxy-host').value, port: Number(el('proxy-port').value), username: el('proxy-user').value, password: el('proxy-password').value, timeout_ms: Number(el('proxy-timeout').value) };
  try { await api('/api/proxy', { method: 'POST', body: settings }); await btConfig(); el('proxy-status').textContent = '出站代理已保存。'; }
  finally { el('proxy-password').value = ''; }
});
onForm('proxy-test-form', async () => {
  try {
    el('proxy-status').textContent = '正在实际建立代理与目标 TCP 连接…';
    const result = await api('/api/proxy/test', { method: 'POST', body: { host: el('proxy-test-host').value, port: Number(el('proxy-test-port').value) } });
    el('proxy-status').textContent = `代理握手和目标 TCP 连接成功，实测 ${result.elapsed_ms} ms；此结果不代表种子能完成下载。`;
  } catch (error) { el('proxy-status').textContent = error.message; }
});

async function btFiles(task) {
  const version = authVersion; const result = await api(`/api/tasks/${task}/files`); if (version !== authVersion) return;
  btFilesTask = task; el('bt-files-panel').hidden = false; el('bt-file-list').replaceChildren();
  el('bt-files-title').textContent = `任务 #${task} 的可播放文件`;
  for (const file of result.files) {
    const row = document.createElement('div'); row.className = 'user-row';
    const text = document.createElement('span'); text.textContent = `${file.name} · 已校验 ${size(file.verified_bytes)} / ${size(file.size)} · 从头连续可用 ${size(file.contiguous_from_start)}`;
    row.append(text, button('边下边播', async () => {
      tab('library'); await play({ ...file, bt: { task, index: file.index } });
      el('playback-status').textContent = result.paused && !file.complete ? '任务已暂停；读取已校验分片，缺少区域需先在 BT 任务中点击恢复。' : '正在读取已校验 BT 分片。定位由浏览器容器索引转换为 Range，服务端据此调整分片优先级。';
    })); el('bt-file-list').append(row);
  }
  if (!result.files.length) el('bt-file-list').textContent = '种子中没有支持的视频文件。';
}
el('refresh-bt-files').addEventListener('click', () => { if (btFilesTask) btFiles(btFilesTask).catch(error => message(error.message)); });
player.addEventListener('playing', () => { if (selected?.bt) { btRetryCount = 0; btCompleteFailures = 0; } });
player.addEventListener('play', () => { if (selected?.bt) btWantsPlay = true; });
player.addEventListener('pause', () => { if (selected?.bt && !btReloading && !player.error) btWantsPlay = false; });
el('retry-bt-playback').addEventListener('click', () => {
  if (!selected?.bt) return; clearTimeout(btRetryTimer); btRetryTimer = null; btRetryCount = 0; btCompleteFailures = 0;
  btReloading = true; btWantsPlay = true; player.load();
});
async function retryBtPlayback() {
  if (!selected?.bt || btRetryTimer) return;
  const selection = selected, version = authVersion;
  if (btRetryCount >= 30) { el('player-error').textContent = '自动重试已达 30 次，请检查任务与网络后点击重试。'; return; }
  if (btLastReloadComplete) btCompleteFailures++;
  btRetryCount++; el('player-error').textContent = `BT 数据暂不可用或浏览器未能解码，正在等待并重试（${btRetryCount}/30）…`;
  btRetryTimer = setTimeout(async () => {
    btRetryTimer = null; if (selected !== selection || version !== authVersion) return;
    try {
      const result = await api(`/api/tasks/${selection.bt.task}/files`);
      if (selected !== selection || version !== authVersion) return;
      const file = result.files.find(item => item.index === selection.bt.index);
      if (!file) throw new Error('视频文件不存在。');
      if (file.integrity_error) throw new Error('分片校验失败，请暂停并重新校验任务。');
      if (result.paused && !file.complete) throw new Error('BT 任务已暂停，请先恢复任务，再点击重试。');
      if (file.complete && btCompleteFailures >= 3) throw new Error('数据已完整，但浏览器未能播放；请检查编码兼容性或重新校验任务。');
      btLastReloadComplete = file.complete; btReloading = true; player.load();
    } catch (error) { if (selected === selection) el('player-error').textContent = error.message; }
  }, 2000);
}

async function refreshRemote() {
  const version = authVersion, identity = user;
  const data = await api('/api/remote');
  if (version !== authVersion || identity !== user) return;
  el('remote-status').textContent = data.adapter === 'reverse_proxy' ? `HTTPS 反向代理入口：${data.public_origin}。探测只证明本机到入口的连通性，未验证跨 NAT。` : '当前为本地 HTTP 模式，请先按部署说明配置 HTTPS 反向代理。';
}
async function refreshShares() {
  const version = authVersion, identity = user;
  const data = await api('/api/shares');
  if (version !== authVersion || identity !== user) return;
  el('share-list').replaceChildren();
  for (const item of data.shares) {
    const row = document.createElement('div'); row.className = 'user-row';
    const text = document.createElement('span'); text.className = 'user-name';
    text.textContent = `#${item.id} · ${item.name} · ${item.revoked ? '已撤销' : item.expires*1000 <= Date.now() ? '已到期' : '有效'} · ${new Date(item.expires*1000).toLocaleString()}`;
    row.append(text);
    if (!item.revoked) row.append(button('撤销', async () => {
      await api(`/api/shares/${item.id}`, {method:'DELETE'}); el('share-url').value = ''; await refreshShares();
    }, true));
    el('share-list').append(row);
  }
}
onForm('create-share-form', async () => {
  const version = authVersion;
  const data = await api('/api/shares', {method:'POST', body:{media_id:Number(el('share-media-id').value), ttl_seconds:Number(el('share-ttl').value), scope:'video_read'}});
  if (version !== authVersion || !user) return;
  el('share-url').value = data.url.startsWith('/') ? location.origin + data.url : data.url;
  message(data.transport === 'local_http_only' ? '已创建本地演示链接；公网分享需要 HTTPS 入口。' : '已创建 HTTPS 分享链接。令牌只显示一次，请妥善转交。');
  await refreshShares();
});
el('copy-share').addEventListener('click', async () => {
  if (!el('share-url').value) { message('请先创建分享链接。'); return; }
  try { await navigator.clipboard.writeText(el('share-url').value); message('链接已复制。'); }
  catch { el('share-url').select(); message('浏览器无法自动复制，请手动复制选中的链接。'); }
});
el('refresh-shares').addEventListener('click', async () => { try { await refreshShares(); } catch(error) { message(error.message); } });
el('test-remote').addEventListener('click', async () => {
  const version = authVersion; el('test-remote').disabled = true;
  el('remote-test-result').textContent = '正在验证 TLS 证书、入口和服务身份…';
  try {
    const data = await api('/api/remote/test', {method:'POST',body:{}});
    if (version !== authVersion) return;
    const states = {reachable:'HTTPS 入口可达，证书和服务身份校验通过。此结果不是跨 NAT 实测。', local_only:'当前仅配置本地 HTTP，尚无远程入口。', tls_error:'TLS 证书信任、域名或握手失败。不会降级为 HTTP。', connection_error:'HTTPS 入口连接失败或超时。请检查代理、网络与域名；不会自动转到 HTTP。', entry_mismatch:'HTTPS 入口未连接到本服务，请检查反向代理配置。'};
    el('remote-test-result').textContent = states[data.state] || data.message;
  } catch(error) { if (version === authVersion) el('remote-test-result').textContent = error.message; }
  finally { el('test-remote').disabled = false; }
});

async function auditPage(before = null, append = false) {
  const version = authVersion, identity = user;
  const result = await api('/api/admin/audit?limit=20' + (before ? `&before_id=${before}` : ''));
  if (version !== authVersion || identity !== user) return;
  if (!append) el('audit-list').replaceChildren();
  for (const item of result.audit) {
    const row = document.createElement('p'); row.className = 'audit-row';
    row.textContent = `#${item.id} · ${new Date(item.timestamp*1000).toLocaleString()} · ${item.event} · 账号 ${item.actor ?? '匿名'} · ${JSON.stringify(item.detail)}`;
    el('audit-list').append(row);
  }
  nextAudit = result.next_before_id; el('older-audit').hidden = nextAudit === null;
}
async function refreshMonitor() {
  const version = authVersion, identity = user;
  const data = await api('/api/admin/status');
  if (version !== authVersion || identity !== user) return;
  const entries = [
    ['服务', `V${data.version} · ${data.mode === 'reverse_proxy' ? 'HTTPS 入口' : '本地 HTTP'}`],
    ['运行时间', `${data.uptime_seconds} 秒`], ['账号', `${data.users.enabled}/${data.users.total} 个启用`],
    ['视频索引', `${data.media.count} 个 · ${size(data.media.catalog_bytes)}`],
    ['BT 任务', `${data.tasks.total} 个 · ${data.tasks.requested_paused} 个请求暂停`],
    ['分享', `${data.shares.active} 个有效 · ${data.shares.revoked} 个已撤销`],
    ['数据卷可用空间', data.data_fs_free_bytes === null ? '读取失败' : size(data.data_fs_free_bytes)],
    ['审计', `${data.audit_entries} 条`]
  ];
  el('monitor-summary').replaceChildren();
  for (const [label, value] of entries) {
    const row = document.createElement('p'); row.textContent = `${label}：${value}`; el('monitor-summary').append(row);
  }
  el('monitor-status').textContent = `快照时间 ${new Date(data.timestamp*1000).toLocaleString()}；按刷新读取，不显示模拟数据。`;
  await auditPage();
}
el('refresh-monitor').addEventListener('click', async () => {
  el('refresh-monitor').disabled = true;
  try { await refreshMonitor(); } catch(error) { el('monitor-status').textContent = error.message; }
  finally { el('refresh-monitor').disabled = false; }
});
el('older-audit').addEventListener('click', async () => {
  if (nextAudit === null) return;
  el('older-audit').disabled = true;
  try { await auditPage(nextAudit, true); } catch(error) { el('monitor-status').textContent = error.message; }
  finally { el('older-audit').disabled = false; }
});
