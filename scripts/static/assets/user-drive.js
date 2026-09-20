(() => {
  const key = 'videoAnalyzer.lanChat.sessionToken.v2';
  const token = () => localStorage.getItem(key) || '';
  const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const paths = {folder:'<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/><path d="M3 9h18"/>',upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 15v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/>',close:'<path d="m6 6 12 12M6 18 18 6"/>',lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',video:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m10 8 6 4-6 4V8Z"/>',file:'<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Z"/><path d="M14 3v6h6M8 14h8M8 17h5"/>',download:'<path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>',trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',refresh:'<path d="M20 4v6h-6M4 20v-6h6M5.5 8a7 7 0 0 1 11.6-3L20 10M4 14l2.9 5A7 7 0 0 0 18.5 16"/>'};
  const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name]}</svg>`;
  const size = bytes => !bytes ? '0 KB' : bytes < 1024 ** 2 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : bytes < 1024 ** 3 ? `${(bytes / 1024 ** 2).toFixed(1)} MB` : `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  const video = f => /\.(mp4|mov|m4v|webm)$/i.test(f.name);
  const date = value => new Date(value * 1000).toLocaleDateString('zh-CN', {month:'2-digit',day:'2-digit'});
  const dialog = document.createElement('dialog');
  dialog.className = 'ud-dialog'; dialog.setAttribute('aria-labelledby', 'ud-title');
  dialog.innerHTML = `<div class="ud-head"><div class="ud-mark">${icon('folder')}</div><div class="ud-title"><h2 id="ud-title">我的网盘</h2><p data-subtitle>让视频随手可用，也让文件井井有条。</p></div><button class="ud-icon-btn" data-close aria-label="关闭网盘">${icon('close')}</button></div>
    <div class="ud-body"><div class="ud-overview"><div class="ud-person"><span class="ud-avatar" data-avatar>·</span><div><div class="ud-owner" data-owner>正在读取账户</div><div class="ud-private">${icon('lock')}仅当前邻聊账户可见</div></div></div><div class="ud-retention">${icon('clock')}保留 7 天 · 到期自动清理</div></div>
    <div class="ud-drop" data-drop><span class="ud-drop-icon">${icon('upload')}</span><div class="ud-drop-copy"><strong>把文件拖到这里</strong><p>支持批量上传，单个文件最大 10 GB</p></div><button class="ud-btn ud-primary" data-upload>${icon('upload')}上传文件</button><input type="file" multiple hidden data-input aria-label="上传网盘文件"></div>
    <div class="ud-progress" data-progress hidden><div class="ud-progress-line"><span class="ud-progress-name" data-progress-name></span><span class="ud-percent" data-percent>0%</span><button class="ud-icon-btn" data-cancel-upload aria-label="取消上传">${icon('close')}</button></div><progress max="100" value="0" aria-label="文件上传进度"></progress><div class="ud-progress-note" data-progress-note></div></div>
    <div class="ud-tools"><div class="ud-tabs" aria-label="文件类型"><button class="ud-tab" data-filter="all" aria-pressed="true">全部文件</button><button class="ud-tab" data-filter="video" aria-pressed="false">视频</button><button class="ud-tab" data-filter="other" aria-pressed="false">其他</button></div><label class="ud-search">${icon('search')}<input type="search" placeholder="搜索文件名" aria-label="搜索网盘文件"></label><button class="ud-icon-btn" data-refresh aria-label="刷新文件列表">${icon('refresh')}</button></div>
    <div class="ud-notice" data-notice role="status" hidden></div><p class="ud-count" data-count></p><div class="ud-table-head" aria-hidden="true"><span>文件名称</span><span>大小</span><span>剩余时间</span><span data-action-title>操作</span></div><div class="ud-files" data-files></div></div>
    <div class="ud-footer"><span class="ud-footer-note" data-footer-note>临时存放，轻装流转。重要文件请另行备份。</span><div class="ud-footer-actions"><button class="ud-btn" data-done>完成</button><button class="ud-btn ud-primary" data-use hidden disabled>使用此视频</button></div></div>`;
  document.body.append(dialog);
  const $ = selector => dialog.querySelector(selector);
  let selectVideo = null, ownerToken = '', files = [], filter = 'all', selected = '', pendingDelete = '', busy = false, xhr = null, cancelled = false, generation = 0, authenticated = false;
  function notice(message = '', error = false) { $('[data-notice]').hidden = !message; $('[data-notice]').textContent = message; $('[data-notice]').classList.toggle('ud-error', error); }
  function ensureOwner() { if (!ownerToken || ownerToken !== token()) throw new Error('请先在邻聊选择账户，或重新打开网盘。'); }
  async function request(path, options = {}) {
    ensureOwner(); const session = ownerToken;
    const response = await fetch(path, {...options, headers:{...options.headers, 'X-Lan-Chat-Token':session}});
    const result = await response.json();
    if (session !== token() || session !== ownerToken) throw new Error('账户已切换，请重新打开网盘。');
    if (!response.ok) throw new Error(result.error || '请求失败，请重试。');
    return result;
  }
  function empty(title, description) { return `<div class="ud-empty"><span class="ud-empty-icon">${icon('folder')}</span><strong>${esc(title)}</strong><p>${esc(description)}</p></div>`; }
  function render() {
    const query = $('input[type=search]').value.trim().toLowerCase();
    const eligible = files.filter(f => !selectVideo || (video(f) && f.size <= 2 * 1024 ** 3));
    const visible = eligible.filter(f => (filter === 'all' || (filter === 'video' ? video(f) : !video(f))) && f.name.toLowerCase().includes(query));
    $('[data-count]').textContent = `${visible.length} 个${selectVideo ? '可用视频' : '文件'} · ${size(visible.reduce((sum, f) => sum + f.size, 0))}`;
    $('[data-files]').innerHTML = visible.map(f => {
      const remaining = f.expires_at - Date.now() / 1000;
      const ttl = remaining <= 0 ? '已过期' : remaining < 86400 ? '不足 1 天' : `${Math.ceil(remaining / 86400)} 天`;
      const suffix = f.name.includes('.') ? f.name.split('.').pop().slice(0, 8) : 'FILE';
      return `<div class="ud-file ${selected === f.id ? 'ud-selected' : ''}" data-id="${esc(f.id)}"><div class="ud-file-main">${selectVideo ? `<input type="radio" name="ud-video" value="${esc(f.id)}" aria-label="选择 ${esc(f.name)}" ${selected === f.id ? 'checked' : ''}>` : ''}<span class="ud-file-icon ${video(f) ? '' : 'ud-other'}">${icon(video(f) ? 'video' : 'file')}</span><div class="ud-file-meta"><div class="ud-file-name" title="${esc(f.name)}">${esc(f.name)}</div><div class="ud-file-sub">${esc(suffix)} · ${date(f.created_at)} 上传<span class="ud-mobile-size"> · ${size(f.size)}</span></div></div></div><span class="ud-file-size">${size(f.size)}</span><span class="ud-expiry ${remaining < 86400 ? 'ud-soon' : ''}">${ttl}<small>${date(f.expires_at)} 到期</small></span><div class="ud-file-actions">${selectVideo ? `<span class="ud-file-size">${selected === f.id ? '已选中' : '选择'}</span>` : `<button class="ud-icon-btn" data-download="${esc(f.id)}" aria-label="下载 ${esc(f.name)}" title="下载">${icon('download')}</button><button class="ud-icon-btn ud-delete" data-delete="${esc(f.id)}" aria-label="删除 ${esc(f.name)}" title="删除">${icon('trash')}</button>`}</div>${pendingDelete === f.id ? '<div class="ud-confirm"><span>删除这个文件？已创建的任务不受影响。</span><button class="ud-btn" data-keep>取消</button><button class="ud-btn ud-danger" data-confirm-delete>确认删除</button></div>' : ''}</div>`;
    }).join('') || empty(query ? '没有找到这个文件' : selectVideo ? '还没有可用的视频' : '这里是你的临时文件空间', query ? '换个关键词，或清空搜索试试。' : selectVideo ? '请先在邻聊网盘上传 MP4、MOV、M4V 或 WebM 视频，单个不超过 2 GB。' : '拖入视频或文件，之后可直接在 Proxy 发布任务中选用。');
    $('[data-use]').disabled = !eligible.some(f => f.id === selected);
    if (selectVideo) $('[data-footer-note]').textContent = selected ? `已选：${files.find(f => f.id === selected)?.name || ''}` : '选中一个视频后，点击“使用此视频”。';
  }
  async function refresh() {
    const run = ++generation;
    $('[data-refresh]').disabled = true;
    try {
      const [profile, result] = await Promise.all([request('/api/lan-chat/bootstrap'), request('/api/lan-chat/drive')]);
      if (run !== generation || !dialog.open) return;
      authenticated = true; files = result.files;
      $('[data-owner]').textContent = profile.currentUser.nickname;
      $('[data-avatar]').textContent = profile.currentUser.nickname.slice(0, 1);
      if (!files.some(f => f.id === selected)) selected = '';
      render();
    } catch (error) {
      if (run !== generation) return;
      authenticated = false; files = []; selected = ''; $('[data-use]').disabled = true;
      $('[data-owner]').textContent = '账户暂不可用'; $('[data-count]').textContent = '';
      $('[data-files]').innerHTML = empty('暂时无法读取网盘', '请检查登录状态或网络，然后点击刷新重试。'); notice(error.message, true);
    } finally { if (run === generation) { $('[data-refresh]').disabled = false; $('[data-upload]').disabled = busy || !authenticated; } }
  }
  function upload(file) {
    return new Promise((resolve, reject) => {
      const session = ownerToken; xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/lan-chat/drive/upload'); xhr.setRequestHeader('X-Lan-Chat-Token', session);
      xhr.upload.onprogress = event => { if (event.lengthComputable) { const value = Math.round(event.loaded / event.total * 100); $('progress').value = value; $('[data-percent]').textContent = value === 100 ? '保存中…' : `${value}%`; } };
      xhr.onload = () => { let result = {}; try { result = JSON.parse(xhr.responseText); } catch (_) {} if (session !== token()) reject(new Error('账户已切换，上传已停止。')); else if (xhr.status >= 200 && xhr.status < 300) resolve(result); else reject(new Error(result.error || '上传失败，请重试。')); };
      xhr.onerror = () => reject(new Error('网络中断，未完成的文件请重新上传。'));
      xhr.onabort = () => reject(new Error('已取消上传。'));
      const form = new FormData(); form.append('file', file); xhr.send(form);
    });
  }
  async function uploadFiles(list) {
    if (busy || !authenticated || selectVideo || !list.length) return;
    busy = true; cancelled = false; $('[data-upload]').disabled = true; $('[data-input]').disabled = true; $('[data-progress]').hidden = false; notice();
    let completed = 0;
    try {
      for (const file of list) {
        ensureOwner(); if (cancelled) break;
        if (!file.size || file.size > 10 * 1024 ** 3) throw new Error(`${file.name}：文件为空或超过 10 GB。`);
        $('[data-progress-name]').textContent = file.name; $('[data-progress-note]').textContent = `第 ${completed + 1} / ${list.length} 个 · ${size(file.size)} · 上传时请勿关闭页面`;
        $('progress').value = 0; $('[data-percent]').textContent = '0%';
        await upload(file); completed++;
      }
      notice(`已上传 ${completed} 个文件，保留至 ${date(Date.now() / 1000 + 7 * 86400)}。`);
    } catch (error) { notice(`${completed ? `已完成 ${completed} 个文件。` : ''}${error.message}`, !cancelled); }
    finally { xhr = null; busy = false; $('[data-progress]').hidden = true; $('[data-input]').disabled = false; $('[data-input]').value = ''; await refresh(); }
  }
  $('[data-upload]').onclick = () => $('[data-input]').click();
  $('[data-input]').onchange = event => uploadFiles([...event.target.files]);
  $('[data-cancel-upload]').onclick = () => { cancelled = true; xhr?.abort(); };
  const drop = $('[data-drop]');
  drop.ondragover = event => { event.preventDefault(); if (!busy) drop.classList.add('ud-dragging'); };
  drop.ondragleave = () => drop.classList.remove('ud-dragging');
  drop.ondrop = event => { event.preventDefault(); drop.classList.remove('ud-dragging'); uploadFiles([...event.dataTransfer.files]); };
  dialog.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => { filter = button.dataset.filter; dialog.querySelectorAll('[data-filter]').forEach(b => b.setAttribute('aria-pressed', String(b === button))); render(); });
  $('input[type=search]').oninput = render;
  $('[data-refresh]').onclick = () => { notice(); refresh(); };
  $('[data-files]').onclick = async event => {
    const row = event.target.closest('[data-id]'); if (!row) return;
    try {
      ensureOwner();
      if (selectVideo) { selected = row.dataset.id; render(); $(`input[value="${selected}"]`)?.focus(); return; }
      if (event.target.closest('[data-delete]')) { pendingDelete = row.dataset.id; render(); return; }
      if (event.target.closest('[data-keep]')) { pendingDelete = ''; render(); return; }
      if (event.target.closest('[data-confirm-delete]')) { event.target.disabled = true; await request(`/api/lan-chat/drive/${row.dataset.id}/delete`, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); pendingDelete = ''; await refresh(); notice('文件已删除。'); }
      if (event.target.closest('[data-download]')) {
        const form = document.createElement('form'); form.method = 'POST'; form.target = '_blank'; form.action = `/api/lan-chat/drive/${row.dataset.id}/download`;
        const input = document.createElement('input'); input.type = 'hidden'; input.name = 'token'; input.value = ownerToken; form.append(input); document.body.append(form); form.submit(); form.remove();
      }
    } catch (error) { notice(error.message, true); }
  };
  $('[data-files]').onchange = event => { if (event.target.matches('input[type=radio]')) { selected = event.target.value; render(); $(`input[value="${selected}"]`)?.focus(); } };
  $('[data-use]').onclick = () => { try { ensureOwner(); const file = files.find(f => f.id === selected); if (!file || file.expires_at <= Date.now() / 1000) throw new Error('视频已过期，请刷新后重新选择。'); selectVideo(file, ownerToken); dialog.close(); } catch (error) { notice(error.message, true); } };
  function close() { if (busy) { notice('文件正在上传，完成后可关闭；也可以点击进度条旁的取消按钮。'); return; } dialog.close(); generation++; }
  $('[data-close]').onclick = close; $('[data-done]').onclick = close;
  dialog.oncancel = event => { if (event.target !== dialog) return; event.preventDefault(); close(); };
  window.openUserDrive = async callback => {
    if (busy) return;
    selectVideo = typeof callback === 'function' ? callback : null; ownerToken = token(); files = []; selected = ''; pendingDelete = ''; authenticated = false; filter = selectVideo ? 'video' : 'all';
    dialog.classList.toggle('ud-picker', Boolean(selectVideo));
    $('[data-action-title]').textContent = selectVideo ? '选择' : '操作';
    $('#ud-title').textContent = selectVideo ? '选择网盘视频' : '我的网盘';
    $('[data-subtitle]').textContent = selectVideo ? '直接使用已上传的视频，无需再次上传。' : '视频与文件，随时存取。';
    $('[data-drop]').hidden = Boolean(selectVideo); $('[data-use]').hidden = !selectVideo; $('[data-use]').disabled = true; $('[data-upload]').disabled = true;
    $('[data-done]').textContent = selectVideo ? '取消' : '完成';
    $('[data-footer-note]').textContent = selectVideo ? '选中一个视频后，点击“使用此视频”。' : '临时存放，重要文件请另行备份。';
    dialog.querySelectorAll('[data-filter]').forEach(b => { b.hidden = Boolean(selectVideo) && b.dataset.filter !== 'video'; b.setAttribute('aria-pressed', String(b.dataset.filter === filter)); });
    $('input[type=search]').value = ''; $('[data-count]').textContent = ''; $('[data-owner]').textContent = '正在读取账户'; $('[data-avatar]').textContent = '·'; notice();
    $('[data-files]').innerHTML = empty('正在读取文件', '马上就好…');
    if (!dialog.open) dialog.showModal(); await refresh();
  };
  window.addEventListener('storage', event => { if (event.key === key) { generation++; cancelled = true; xhr?.abort(); dialog.close(); files = []; $('[data-files]').replaceChildren(); } });
  window.addEventListener('beforeunload', event => { if (busy) { event.preventDefault(); event.returnValue = ''; } });
  document.querySelectorAll('[onclick="openUserDrive()"], [onclick="chooseDriveVideo()"]').forEach(button => { button.classList.add('ud-entry'); const label = button.textContent; button.innerHTML = `${icon('folder')}<span>${esc(label)}</span>`; button.setAttribute('aria-label', label === '网盘' ? '我的网盘' : label); });
})();
