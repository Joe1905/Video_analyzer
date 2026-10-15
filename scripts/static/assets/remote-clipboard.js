(() => {
  const frame = document.getElementById('browser');
  const dialog = document.getElementById('clipboard-dialog');
  const text = document.getElementById('clipboard-text');
  const image = document.getElementById('clipboard-image');
  const download = document.getElementById('clipboard-download');
  const status = document.getElementById('clip-status');
  const help = document.getElementById('clipboard-help');
  const api = '/api/remote-browser/clipboard';
  const limit = 16 * 1024 * 1024;
  let staged = null;
  let imageUrl = '';
  let copyTimer;
  const report = message => { status.textContent = message; };
  const explain = () => {
    help.textContent = window.isSecureContext
      ? '远程 → 本机：取回远程剪贴板，再点“复制到本机”。'
      : '远程 → 本机：取回后，文字点“复制到本机”再按 Ctrl+C；图片在预览上右键选择“复制图片”。内嵌浏览器没有该菜单时可下载图片。当前为 HTTP 地址，HTTPS 支持直接写入本机剪贴板。';
  };
  const focusRemote = () => frame.contentDocument?.querySelector('#noVNC_container canvas')?.focus({preventScroll:true});

  function show(blob) {
    staged = blob;
    if (imageUrl) URL.revokeObjectURL(imageUrl);
    imageUrl = '';
    image.hidden = !blob.type.startsWith('image/');
    download.hidden = image.hidden;
    if (!image.hidden) {
      text.value = '';
      imageUrl = URL.createObjectURL(blob);
      image.src = imageUrl;
      download.href = imageUrl;
    } else {
      image.removeAttribute('src');
      download.removeAttribute('href');
    }
  }

  async function checked(response) {
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || '剪贴板操作失败，请重试');
    }
    return response;
  }

  async function png(file) {
    if (file.size > limit) throw new Error('剪贴板内容不能超过 16 MB');
    if (file.type === 'image/png') return file;
    const bitmap = await createImageBitmap(file);
    try {
      if (bitmap.width * bitmap.height > 32000000) throw new Error('图片不能超过 3200 万像素');
      const canvas = document.createElement('canvas');
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.getContext('2d').drawImage(bitmap, 0, 0);
      return await new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('图片转换失败')), 'image/png'));
    } finally {
      bitmap.close();
    }
  }

  async function pasteData(data) {
    const item = Array.from(data.items || []).find(item => item.type.startsWith('image/'));
    if (item) return png(item.getAsFile());
    return new Blob([data.getData('text/plain')], {type:'text/plain;charset=utf-8'});
  }

  async function send(blob) {
    if (blob.size > limit) throw new Error('剪贴板内容不能超过 16 MB');
    report('正在粘贴到远程浏览器…');
    await checked(await fetch(api + '?paste=1', {method:'POST',
      headers:{'Content-Type':blob.type, 'X-Remote-Clipboard':'1'}, body:blob}));
    report(blob.type.startsWith('image/') ? '图片已粘贴到远程浏览器' : '文字已粘贴到远程浏览器');
  }

  async function copyLocal(blob) {
    if (navigator.clipboard?.write && window.ClipboardItem) {
      try {
        const mime = blob.type.startsWith('image/') ? 'image/png' : 'text/plain';
        const content = mime === 'text/plain' ? new Blob([await blob.text()], {type:mime}) : blob;
        await navigator.clipboard.write([new ClipboardItem({[mime]:content})]);
        report('已复制到本机剪贴板');
        return;
      } catch (_) { /* Keep a native copy path when the API is unavailable. */ }
    }
    if (!blob.type.startsWith('image/')) {
      text.value = await blob.text();
      if (!dialog.open) dialog.showModal();
      text.focus();
      text.select();
      help.textContent = '文字已选中，请按 Ctrl+C 复制到本机剪贴板';
      report(help.textContent);
      return;
    }
    throw new Error('当前地址不能直接写入本机图片剪贴板，请打开剪贴板面板，在图片预览上右键选择“复制图片”');
  }

  async function receive(auto = false) {
    const response = await checked(await fetch(api, {headers:{'X-Remote-Clipboard':'1'}, cache:'no-store'}));
    const blob = await response.blob();
    show(blob);
    if (!blob.type.startsWith('image/')) text.value = await blob.text();
    report(blob.type.startsWith('image/') ? '已取回远程图片，可在剪贴板面板中复制到本机' : '已取回远程文字');
    if (auto) {
      if (navigator.clipboard?.write) {
        try { await copyLocal(blob); } catch (error) { report(error.message); }
      } else {
        report('远程剪贴板已取回，点击顶部“剪贴板”可复制到本机');
      }
      focusRemote();
    }
  }

  function attach() {
    const doc = frame.contentDocument;
    if (!doc?.body || doc.getElementById('remote-paste-sink')) return;
    const sink = doc.createElement('textarea');
    sink.id = 'remote-paste-sink';
    sink.setAttribute('aria-hidden', 'true');
    sink.tabIndex = -1;
    sink.style.cssText = 'position:fixed;left:-10000px;top:0';
    doc.body.append(sink);
    doc.addEventListener('click', event => {
      if (!event.target.closest('#noVNC_clipboard_button')) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      explain();
      dialog.showModal();
      text.focus();
    }, true);
    doc.addEventListener('keydown', event => {
      if (!(event.ctrlKey || event.metaKey)) return;
      const key = event.key.toLowerCase();
      if (key === 'v' && !event.shiftKey) {
        // Preserve the native paste event, but stop noVNC forwarding Ctrl+V early.
        event.stopImmediatePropagation();
        sink.focus({preventScroll:true});
      } else if (key === 'c' || key === 'x') {
        clearTimeout(copyTimer);
        copyTimer = setTimeout(() => receive(true).catch(error => report(error.message)), 350);
      }
    }, true);
    doc.addEventListener('paste', event => {
      if (!event.clipboardData) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      pasteData(event.clipboardData).then(send).catch(error => report(error.message)).finally(() => {
        sink.value = '';
        focusRemote();
      });
    }, true);
  }

  frame.addEventListener('load', attach);
  if (frame.contentDocument?.readyState === 'complete') attach();
  document.getElementById('clipboard').onclick = () => { explain(); dialog.showModal(); text.focus(); };
  document.getElementById('clipboard-close').onclick = () => { dialog.close(); focusRemote(); };
  text.addEventListener('input', () => { show(new Blob([text.value], {type:'text/plain;charset=utf-8'})); });
  text.addEventListener('paste', event => {
    event.preventDefault();
    pasteData(event.clipboardData).then(async blob => {
      show(blob);
      if (!blob.type.startsWith('image/')) text.value = await blob.text();
      report('内容已就绪，点击“发送到远程”粘贴');
    }).catch(error => report(error.message));
  });
  document.getElementById('clipboard-send').onclick = async () => {
    try {
      await send(staged || new Blob([text.value], {type:'text/plain;charset=utf-8'}));
      dialog.close();
      focusRemote();
    } catch (error) { report(error.message); }
  };
  document.getElementById('clipboard-fetch').onclick = () => receive().catch(error => report(error.message));
  document.getElementById('clipboard-copy').onclick = () => {
    if (staged) copyLocal(staged).catch(error => { help.textContent = error.message; });
  };
})();
