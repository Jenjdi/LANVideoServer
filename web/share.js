'use strict';
const shareEl = id => document.getElementById(id), sharedPlayer = shareEl('shared-player');
let expiry = 0, poll = null;
async function shareApi(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options });
  const data = await response.json();
  if (!response.ok) {
    if ([401,404].includes(response.status)) throw new Error('分享链接已过期、撤销或不可用，请向管理员索取新链接。');
    throw new Error(`读取失败（${response.status}）：${data.error || '请重试'}`);
  }
  return data;
}
function unavailable(error) {
  clearInterval(poll); poll = null; sharedPlayer.pause(); sharedPlayer.removeAttribute('src'); sharedPlayer.load();
  shareEl('share-panel').hidden = true; shareEl('share-status').textContent = error.message;
}
async function info() {
  const data = await shareApi('/share/api/info'); expiry = data.expires;
  shareEl('share-name').textContent = data.name;
  shareEl('share-details').textContent = `${data.format.toUpperCase()} · ${(data.size/1048576).toFixed(2)} MiB · 到期时间 ${new Date(expiry*1000).toLocaleString()}`;
  return data;
}
async function start() {
  // Fragments never enter HTTP paths or proxy access logs. Remove from browser history immediately.
  let token = location.hash.slice(1); history.replaceState(null, '', '/share');
  shareEl('share-transport').textContent = location.protocol === 'https:' ? '当前通过 HTTPS 入口播放。此页面不会转到 HTTP 连接。' : '当前为本地 HTTP 演示；请勿通过此连接在公网分享，正式部署必须使用 HTTPS。';
  try {
    if (token) {
      if (!/^[0-9a-f]{64}$/.test(token)) throw new Error('分享链接格式不正确。');
      await shareApi('/share/api/exchange', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({token}) });
      token = '';
    }
    await info(); shareEl('share-panel').hidden = false; sharedPlayer.src = '/share/media'; sharedPlayer.load();
    shareEl('share-status').textContent = '授权已确认，可以播放或拖动进度条。';
    poll = setInterval(async () => {
      try { if (Date.now()/1000 >= expiry) throw new Error('分享链接已到期。'); await info(); }
      catch(error) { unavailable(error); }
    },5000);
  } catch(error) { token = ''; unavailable(error); }
}
sharedPlayer.addEventListener('error', async () => {
  try { await info(); shareEl('share-status').textContent = '视频未能解码或传输中断，请检查网络与格式后重试。'; }
  catch(error) { unavailable(error); }
});
shareEl('share-retry').addEventListener('click', async () => {
  try { await info(); sharedPlayer.load(); shareEl('share-status').textContent = '正在重新读取视频…'; }
  catch(error) { unavailable(error); }
});
window.addEventListener('pagehide', () => { clearInterval(poll); sharedPlayer.pause(); });
start();
