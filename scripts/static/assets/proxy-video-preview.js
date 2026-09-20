// Selected media stays local or streams through a short-lived drive preview URL.
(() => {
  const sources = document.querySelector('.publish-sources');
  const card = document.createElement('div');
  card.className = 'publish-video-card'; card.hidden = true;
  card.innerHTML = '<div class="publish-video-frame"><video id="publishPreviewVideo" preload="auto" playsinline></video><button type="button" id="publishPreviewPlay" aria-label="播放视频预览" title="播放预览"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 11 7-11 7Z" fill="currentColor"/></svg></button></div><div class="publish-video-info"><span class="publish-video-caption">视频详情</span><strong id="publishPreviewName"></strong><span id="publishPreviewDuration">正在读取时长…</span><span id="publishPreviewError" role="status"></span></div><button type="button" id="publishPreviewCancel" aria-label="取消视频" title="取消视频"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button>';
  sources.after(card);
  const video = document.getElementById('publishPreviewVideo');
  const play = document.getElementById('publishPreviewPlay');
  const name = document.getElementById('publishPreviewName');
  const duration = document.getElementById('publishPreviewDuration');
  const error = document.getElementById('publishPreviewError');
  const player = document.createElement('dialog');
  player.className = 'publish-player'; player.setAttribute('aria-labelledby', 'publishPlayerTitle');
  player.innerHTML = '<div class="publish-player-head"><strong id="publishPlayerTitle"></strong><button type="button" aria-label="关闭播放窗口" title="关闭"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button></div><video controls playsinline preload="metadata"></video><p role="status" hidden></p>';
  document.body.append(player);
  const playerVideo = player.querySelector('video');
  const playerMessage = player.querySelector('p');
  function stopPlayer() { playerVideo.pause(); playerVideo.removeAttribute('src'); playerVideo.removeAttribute('poster'); playerVideo.load(); }
  function closePlayer() { if (player.open) player.close(); stopPlayer(); }
  player.querySelector('button').onclick = closePlayer;
  player.onclose = stopPlayer;
  player.oncancel = event => { if (event.target !== player) return; event.preventDefault(); closePlayer(); };
  playerVideo.onerror = () => { if (player.open) { playerMessage.hidden = false; playerMessage.textContent = '视频暂时无法播放，请关闭后重试或重新选择视频。'; } };
  let objectUrl = '', revision = 0, controller = null;
  function release() {
    closePlayer();
    revision++; controller?.abort(); controller = null;
    video.onloadedmetadata = video.onloadeddata = video.onerror = null;
    video.pause(); video.removeAttribute('src'); video.removeAttribute('poster'); video.load();
    if (objectUrl) URL.revokeObjectURL(objectUrl); objectUrl = '';
    video.controls = false; play.hidden = false; play.disabled = true;
  }
  window.clearPublishPreview = () => { release(); card.hidden = true; sources.hidden = false; };
  function prepare(filename) {
    release(); sources.hidden = true; card.hidden = false;
    name.textContent = filename; name.title = filename; duration.textContent = '正在读取时长…'; error.textContent = '';
    return revision;
  }
  function load(url, current) {
    if (current !== revision) return;
    video.onloadedmetadata = () => {
      if (current !== revision) return;
      const seconds = Math.floor(video.duration);
      duration.textContent = Number.isFinite(seconds) ? `时长 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : '暂时无法读取时长';
    };
    video.onloadeddata = () => {
      if (current !== revision) return;
      play.disabled = false;
      // Capture the decoded first frame before playback, including for local files.
      try {
        const canvas = document.createElement('canvas');
        canvas.width = Math.min(video.videoWidth, 640);
        canvas.height = Math.round(canvas.width * video.videoHeight / video.videoWidth);
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        video.poster = canvas.toDataURL('image/jpeg', 0.85);
      } catch (_) { /* The video element itself still displays the first frame. */ }
      video.onloadeddata = null;
    };
    video.onerror = () => {
      if (current !== revision) return;
      error.textContent = '预览暂不可用，文件可能已过期或浏览器不支持此编码；可取消后重新选择。';
      if (!Number.isFinite(video.duration)) duration.textContent = '时长暂不可用';
      play.disabled = true;
    };
    video.src = url;
  }
  window.showPublishPreview = ({name: filename, url, file}) => {
    const current = prepare(filename);
    if (file) { objectUrl = URL.createObjectURL(file); url = objectUrl; }
    load(url, current);
  };
  window.showPublishDrivePreview = async (file, token) => {
    const current = prepare(file.name); controller = new AbortController();
    try {
      const response = await fetch(`/api/lan-chat/drive/${file.id}/preview`, {method:'POST', headers:{'X-Lan-Chat-Token':token}, signal:controller.signal});
      const payload = await response.json();
      if (current !== revision) return;
      if (!response.ok) throw new Error(payload.error || '读取视频失败');
      if (token !== localStorage.getItem('videoAnalyzer.lanChat.sessionToken.v2')) throw new Error('账户已切换，请重新选择视频');
      load(payload.url, current);
    } catch (failure) { if (current === revision && failure.name !== 'AbortError') { duration.textContent = '时长暂不可用'; error.textContent = failure.message; } }
  };
  play.onclick = async () => {
    if (!video.currentSrc || play.disabled) return;
    document.getElementById('publishPlayerTitle').textContent = name.textContent;
    playerMessage.hidden = true; playerMessage.textContent = '';
    playerVideo.src = video.currentSrc; playerVideo.poster = video.poster;
    if (!player.open) player.showModal();
    try { await playerVideo.play(); }
    catch (_) { if (player.open) { playerMessage.hidden = false; playerMessage.textContent = '请点击播放按钮重试。'; } }
  };
  document.getElementById('publishPreviewCancel').onclick = () => {
    if (state.publishBusy) { error.textContent = '正在创建任务，请完成后再取消视频。'; return; }
    clearPublishPreview(); state.driveFile = null; state.driveToken = ''; state.publishEditing = '';
    document.getElementById('publishVideo').value = '';
    document.getElementById('publishFileLabel').textContent = 'MP4、MOV、M4V、WebM，最大 2GB';
    document.getElementById('publishDiscardEdit').style.display = 'none';
    renderPublishJobs();
  };
  const reset = resetPublishForm;
  resetPublishForm = function() { clearPublishPreview(); reset(); };
  const local = syncPublishFile;
  syncPublishFile = function() { local(); const file = document.getElementById('publishVideo').files[0]; if (file) showPublishPreview({name:file.name, file}); else clearPublishPreview(); };
  const edit = editPublishJob;
  editPublishJob = function(id) { edit(id); const job = state.publishJobs.find(j => j.id === id); if (job) showPublishPreview({name:job.original_name, url:job.preview_url}); };
  const close = closePublishModal;
  closePublishModal = function() { close(); if (!state.publishBusy) clearPublishPreview(); };
  window.addEventListener('storage', event => { if (event.key === 'videoAnalyzer.lanChat.sessionToken.v2' && state.driveFile) { clearPublishPreview(); state.driveFile = null; state.driveToken = ''; } });
})();
