(() => {
  const key = 'videoAnalyzer.lanChat.sessionToken.v2';
  const token = () => localStorage.getItem(key) || '';
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const size = value => `${(value / 1024 / 1024).toFixed(1)} MB`;
  async function request(path, options = {}) {
    const session = token();
    if (!session) throw new Error('请先到邻聊选择用户，再使用网盘。');
    const response = await fetch(path, {...options, headers: {...options.headers, 'X-Lan-Chat-Token': session}});
    const result = await response.json();
    if (session !== token()) throw new Error('用户已切换，请重新打开网盘。');
    if (!response.ok) throw new Error(result.error || '网盘请求失败');
    return result;
  }
  const dialog = document.createElement('dialog');
  dialog.style.cssText = 'width:min(680px,94vw);max-height:85vh;border:1px solid #ddd;border-radius:16px;padding:24px;color:#132238;overflow:auto';
  dialog.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center"><h2 style="margin:0">我的网盘</h2><button type="button" data-close aria-label="关闭网盘">关闭</button></div><p data-owner></p><p>文件自上传起保留 7 天，到期自动清理。仅当前用户可见。</p><label data-upload>上传文件（最大 10GB） <input type="file" multiple></label><p role="status" data-status></p><div data-files></div>';
  document.body.append(dialog);
  const status = dialog.querySelector('[data-status]');
  let selectVideo = null, busy = false, ownerToken = '';
  dialog.querySelector('[data-close]').onclick = () => dialog.close();
  async function render() {
    const [profile, result] = await Promise.all([request('/api/lan-chat/bootstrap'), request('/api/lan-chat/drive')]);
    if (ownerToken !== token()) throw new Error('用户已切换，请重新打开网盘。');
    dialog.querySelector('[data-owner]').textContent = `当前用户：${profile.currentUser.nickname}`;
    const files = result.files.filter(f => !selectVideo || (/\.(mp4|mov|m4v|webm)$/i.test(f.name) && f.size <= 2 * 1024 ** 3));
    dialog.querySelector('[data-files]').innerHTML = files.map(f => `<div style="border-top:1px solid #eee;padding:14px 0;display:flex;gap:12px;align-items:center"><div style="flex:1;min-width:0;overflow-wrap:anywhere"><strong>${escape(f.name)}</strong><div>${size(f.size)} · 到期 ${escape(new Date(f.expires_at * 1000).toLocaleString('zh-CN'))}</div></div>${selectVideo ? `<button type="button" data-select="${f.id}">选用</button>` : `<button type="button" data-download="${f.id}">下载</button><button type="button" data-delete="${f.id}">删除</button>`}</div>`).join('') || '<p>暂无文件。可先在邻聊网盘上传视频。</p>';
    dialog.querySelectorAll('[data-select]').forEach(button => button.onclick = () => {
      if (ownerToken !== token()) { status.textContent = '用户已切换，请重新打开网盘。'; return; }
      selectVideo(files.find(f => f.id === button.dataset.select), ownerToken);
      dialog.close();
    });
    dialog.querySelectorAll('[data-download]').forEach(button => button.onclick = () => {
      if (ownerToken !== token()) return;
      const form = document.createElement('form');
      form.method = 'POST'; form.target = '_blank'; form.action = `/api/lan-chat/drive/${button.dataset.download}/download`;
      const input = document.createElement('input'); input.type = 'hidden'; input.name = 'token'; input.value = ownerToken;
      form.append(input); document.body.append(form); form.submit(); form.remove();
    });
    dialog.querySelectorAll('[data-delete]').forEach(button => button.onclick = async () => {
      if (ownerToken !== token() || !confirm('删除此网盘文件？已创建的 Proxy 任务不受影响。')) return;
      try { await request(`/api/lan-chat/drive/${button.dataset.delete}/delete`, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'}); await render(); }
      catch (error) { status.textContent = error.message; }
    });
  }
  dialog.querySelector('input').onchange = async event => {
    if (busy) return;
    busy = true; event.target.disabled = true;
    try {
      for (const file of event.target.files) {
        if (ownerToken !== token()) throw new Error('用户已切换，请重新打开网盘。');
        if (file.size > 10 * 1024 ** 3) throw new Error(`${file.name} 超过 10GB 限制`);
        status.textContent = `正在上传 ${file.name}（${size(file.size)}），请勿关闭页面…`;
        const form = new FormData(); form.append('file', file);
        await request('/api/lan-chat/drive/upload', {method:'POST', body:form});
      }
      status.textContent = '上传完成'; await render();
    } catch (error) { status.textContent = error.message; }
    finally { busy = false; event.target.disabled = false; event.target.value = ''; }
  };
  window.openUserDrive = async callback => {
    if (busy) { if (!dialog.open) dialog.showModal(); return; }
    selectVideo = typeof callback === 'function' ? callback : null; ownerToken = token();
    dialog.querySelector('[data-upload]').hidden = Boolean(selectVideo);
    dialog.querySelector('[data-files]').replaceChildren(); dialog.querySelector('[data-owner]').textContent = '';
    status.textContent = '正在读取…'; if (!dialog.open) dialog.showModal();
    try { await render(); status.textContent = selectVideo ? '选择用于 Proxy 任务的视频（最大 2GB）。' : ''; }
    catch (error) { status.textContent = error.message; }
  };
  window.addEventListener('storage', event => { if (event.key === key) { dialog.close(); dialog.querySelector('[data-files]').replaceChildren(); } });
})();
