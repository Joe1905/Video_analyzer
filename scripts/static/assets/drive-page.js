(() => {
  const key = 'videoAnalyzer.lanChat.sessionToken.v2';
  const $ = id => document.getElementById(id);
  const login = $('driveLogin');
  let owners = [], owner = null, busy = false;
  async function request(path, body) {
    const response = await fetch('/api/lan-chat/' + path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '请求失败，请重试。');
    return result;
  }
  function row(name, detail, action, user = null) {
    const button = document.createElement('button'); button.type = 'button';
    if (user) {
      const avatar = document.createElement('span'), fallback = document.createElement('span');
      avatar.className = 'drive-login-avatar'; fallback.textContent = (name || '我').trim().slice(0, 1) || '我'; avatar.append(fallback);
      if (user.avatarUrl) {
        const image = document.createElement('img'); image.alt = `${name}的头像`; image.hidden = true;
        image.onload = () => { image.hidden = false; fallback.hidden = true; };
        image.onerror = () => { image.hidden = true; fallback.hidden = false; };
        image.src = user.avatarUrl; avatar.append(image);
      }
      button.append(avatar);
    }
    const copy = document.createElement('span'), label = document.createElement('span'), note = document.createElement('small');
    copy.className = 'drive-login-copy'; label.textContent = name; note.textContent = detail; copy.append(label, note); button.append(copy);
    button.onclick = () => run(action); return button;
  }
  async function run(action) {
    if (busy) return;
    busy = true; $('driveLoginError').textContent = '';
    try { await action(); } catch (error) { $('driveLoginError').textContent = error.message; }
    finally { busy = false; }
  }
  function showOwners() {
    owner = null; $('driveLoginTitle').textContent = '选择云盘账户'; $('driveLoginBack').hidden = true; $('driveCreate').hidden = true;
    $('driveLoginList').replaceChildren(...owners.map(item => row(item.name, `${item.accounts.length} 个设备账户`, () => showAccounts(item), item)));
    if (!owners.length) $('driveLoginList').textContent = '暂无可用账户，请先完成飞书用户同步。';
  }
  function showAccounts(item) {
    owner = item; $('driveLoginTitle').textContent = item.name; $('driveLoginBack').hidden = false; $('driveCreate').hidden = false; $('driveNickname').value = '';
    $('driveLoginList').replaceChildren(...item.accounts.map(account => row(account.nickname, account.online ? '当前在线' : '设备账户', async () => finish(await request('select-account', {feishuUserId:item.id, accountId:account.id})), account)));
    if (!item.accounts.length) $('driveLoginList').textContent = '还没有设备账户，可在下方新建。';
  }
  async function finish(payload) {
    localStorage.setItem(key, payload.sessionToken);
    $('driveAccount').textContent = '切换账户'; login.close();
    await window.openUserDrive();
  }
  async function choose() {
    if (window.isUserDriveBusy?.()) return;
    $('driveLoginClose').hidden = !localStorage.getItem(key); $('driveLoginError').textContent = '';
    if (!login.open) login.showModal();
    $('driveLoginList').textContent = '正在读取账户…';
    try { owners = (await request('login-options')).feishuUsers || []; showOwners(); }
    catch (error) { $('driveLoginList').textContent = ''; $('driveLoginError').textContent = error.message; const retry = row('重新读取账户', '', choose); $('driveLoginList').append(retry); }
  }
  $('driveAccount').onclick = choose;
  $('driveLoginBack').onclick = showOwners;
  $('driveLoginClose').onclick = () => login.close();
  login.oncancel = event => { if (!localStorage.getItem(key) || busy) event.preventDefault(); };
  $('driveCreate').onsubmit = event => { event.preventDefault(); const nickname = $('driveNickname').value.trim(); if (!owner || !nickname) return; run(async () => finish(await request('accounts', {feishuUserId:owner.id, nickname}))); };
  window.addEventListener('storage', event => { if (event.key === key) { if (event.newValue) window.openUserDrive(); else choose(); } });
  async function init() {
    if (!localStorage.getItem(key)) { await choose(); return; }
    const response = await fetch('/api/lan-chat/bootstrap', {headers:{'X-Lan-Chat-Token':localStorage.getItem(key)}});
    if (response.status === 401) { localStorage.removeItem(key); await choose(); return; }
    $('driveAccount').textContent = '切换账户'; await window.openUserDrive();
  }
  init().catch(error => { $('driveMount').textContent = error.message; choose(); });
})();
