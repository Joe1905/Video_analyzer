/* Buffer owns publishing times; this page displays durable local task receipts. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {mode:'schedule', filter:'all', jobs:[], asset:null, status:null, channels:[], requestId:null, busy:false};
  const names = {queued:'等待处理',uploading:'上传中',submitting:'提交中',scheduled:'已定时',sending:'发布中',sent:'已发布',failed:'失败',uncertain:'结果待确认',draft:'Buffer 草稿',needs_approval:'待审批',cancelled:'已取消',cancelling:'取消中',editing:'修改中'};
  const active = ['queued','uploading','submitting','scheduled','sending','cancelling','editing'];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const requestId = () => {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6]=(bytes[6]&15)|64; bytes[8]=(bytes[8]&63)|128;
    const h=Array.from(bytes,x=>x.toString(16).padStart(2,'0')).join('');
    return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
  };
  async function api(path, data) {
    const response = await fetch('/api/publish/'+path, data === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '操作失败');
    return result;
  }
  const zoneName = zone => ({'America/Los_Angeles':'洛杉矶时间','Asia/Shanghai':'北京时间'}[zone] || zone);
  function offset(value, zone) {
    return new Intl.DateTimeFormat('en',{timeZone:zone,timeZoneName:'shortOffset'}).formatToParts(new Date(value)).find(p=>p.type==='timeZoneName').value.replace('GMT','UTC');
  }
  function format(value, zone=state.status?.timezone || 'America/Los_Angeles', labelled=true) {
    if (!value) return '—';
    const date=new Date(typeof value==='number' ? value*1000 : value);
    const text=new Intl.DateTimeFormat('zh-CN',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(date);
    return text+(labelled?`（${zoneName(zone)}，${offset(date,zone)}）`:'');
  }
  function renderClock() {
    if(!state.status)return;
    const now=new Date(state.serverEpoch+performance.now()-state.serverReceived);
    const zone=state.status.timezone;
    $('serverTime').textContent=`当前${zoneName(zone)}：${format(now,zone,false)}（${offset(now,zone)}）`;
  }
  let previewTimer, previewSequence=0;
  async function previewTime() {
    const sequence=++previewSequence, value=$('publishTime').value;
    const zone=state.status?.timezone || 'America/Los_Angeles';
    $('beijingTime').classList.remove('error');
    if(!value){
      $('beijingTime').textContent='北京时间：选择发布时间后显示';
      $('timezone').textContent='';
      return;
    }
    $('beijingTime').textContent='北京时间：换算中…';
    try{
      const result=await api('time-preview?local_time='+encodeURIComponent(value));
      if(sequence!==previewSequence)return;
      $('timezone').textContent=offset(result.utc,result.timezone);
      $('beijingTime').textContent=`北京时间：${format(result.utc,'Asia/Shanghai',false)}（UTC+8）`;
    }catch(e){
      if(sequence!==previewSequence)return;
      $('timezone').textContent='';
      $('beijingTime').textContent=`${zoneName(zone)}：${e.message}`;
      $('beijingTime').classList.add('error');
    }
  }
  function ready() {
    $('submit').disabled = state.busy || !state.asset || !$('channel').value || !state.status?.buffer_configured || !state.status?.media_configured;
    $('videoFile').disabled = state.busy;
  }
  async function loadStatus() {
    state.status = await api('status');
    state.serverEpoch=Date.parse(state.status.now);
    state.serverReceived=performance.now();
    $('connectionDot').classList.toggle('ok', state.status.buffer_configured);
    $('connectionLabel').textContent = state.status.buffer_configured ? 'Buffer 已配置' : '连接 Buffer';
    $('storageDot').classList.toggle('ok', state.status.media_configured);
    $('storageState').textContent = state.status.media_configured ? 'ruitech.hk · 已配置' : '尚未配置';
    $('publishTimeLabel').textContent=`发布时间（${zoneName(state.status.timezone)}）`;
    renderClock();
    previewTime();
    renderJobs();
    ready();
  }
  async function loadChannels() {
    if (!state.status?.buffer_configured) return;
    $('reloadChannels').disabled=true;
    try {
      const previous=$('channel').value;
      state.channels=(await api('channels')).channels;
      $('channel').innerHTML='<option value="">选择 TikTok 频道</option>'+state.channels.map(c=>`<option value="${esc(c.id)}" ${c.isDisconnected||c.isLocked||!c.allowedActions.includes('scheduleUpdates')?'disabled':''}>${esc(c.displayName||c.name)} · ${esc(c.organization)}${c.isDisconnected?'（需重新连接）':c.isLocked?'（已锁定）':c.isQueuePaused?'（已暂停）':''}</option>`).join('');
      if (!state.channels.length) $('channel').innerHTML='<option value="">请先在 Buffer 连接 TikTok 账号</option>';
      if(state.channels.some(c=>c.id===previous)) $('channel').value=previous;
    } finally { $('reloadChannels').disabled=false;ready(); }
  }
  function renderJobs() {
    const filtered=state.jobs.filter(j=>state.filter==='all'||(state.filter==='active'?active.includes(j.state):state.filter==='failed'?['failed','uncertain','needs_approval'].includes(j.state):j.state===state.filter));
    $('jobCount').textContent=state.jobs.length;
    $('jobs').innerHTML=filtered.length ? filtered.map(j=>`<article class="job"><div class="job-top"><strong>${esc(j.channel_name)}</strong><span class="badge ${esc(j.state)}">${esc(names[j.state]||j.state)}</span></div><p>${esc(j.text.length>150?j.text.slice(0,150)+'…':j.text)}</p><div class="job-foot"><span>${j.due_at?'计划 '+format(j.due_at,j.timezone):j.mode==='draft'?'存入草稿 · '+format(j.created,j.timezone):'立即发布 · '+format(j.created,j.timezone)}</span><div class="job-actions">${['queued','draft','scheduled'].includes(j.state)?`<button data-action="edit" data-id="${j.id}">编辑</button>`:''}<button data-action="report" data-id="${j.id}">查看日志</button>${j.post_id&&!['cancelled','cancelling','submitting','editing'].includes(j.state)?`<button data-action="refresh" data-id="${j.id}">查状态</button>`:''}${['queued','draft','scheduled','needs_approval'].includes(j.state)?`<button data-action="cancel" data-id="${j.id}">取消</button>`:''}</div></div></article>`).join('') : '<div class="empty"><svg viewBox="0 0 24 24"><rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h5M8 16h3"/></svg><strong>暂无发布记录</strong><span>创建任务后，在这里查看进度和日志</span></div>';
    if($('report').open && $('report').dataset.jobId) showReport($('report').dataset.jobId, false);
  }
  async function loadJobs() {state.jobs=(await api('jobs')).jobs;$('listError').textContent='';renderJobs();}
  let editing=null, editBusy=false, editPreviewPending=false, editPreviewSequence=0, editPreviewTimer;
  function localInput(value,zone) {
    const parts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value)).map(p=>[p.type,p.value]));
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  }
  async function previewEditTime() {
    const sequence=++editPreviewSequence;
    if(!editing||!editing.can_edit_time)return;
    editPreviewPending=true;$('saveEdit').disabled=true;$('editOffset').textContent='';
    $('editBeijing').classList.remove('error');
    if(!$('editTime').value){$('editBeijing').textContent='请选择发布时间';return;}
    $('editBeijing').textContent='北京时间：换算中…';
    try{
      const result=await api('time-preview?local_time='+encodeURIComponent($('editTime').value)+'&timezone='+encodeURIComponent(editing.job.timezone));
      if(sequence!==editPreviewSequence)return;
      $('editOffset').textContent=offset(result.utc,result.timezone);
      $('editBeijing').textContent=`北京时间：${format(result.utc,'Asia/Shanghai',false)}（UTC+8）`;
      editPreviewPending=false;$('saveEdit').disabled=editBusy;
    }catch(err){if(sequence===editPreviewSequence){$('editBeijing').textContent=err.message;$('editBeijing').classList.add('error');}}
  }
  async function openEdit(id) {
    editing=await api('edit?id='+encodeURIComponent(id));
    const j=editing.job;
    editPreviewPending=false;$('editError').textContent='';$('saveEdit').disabled=false;
    $('editChannel').textContent=j.channel_name+' · '+(names[j.state]||j.state);
    $('editCaption').value=j.text;$('editCaption').disabled=!editing.can_edit_text;
    $('editSchedule').hidden=!editing.can_edit_time;$('editTime').required=editing.can_edit_time;
    $('editTimeLabel').textContent=`发布时间（${zoneName(j.timezone)}）`;
    editing.originalTime=editing.can_edit_time&&j.due_at?localInput(j.due_at,j.timezone):'';
    $('editTime').value=editing.originalTime;$('editOffset').textContent='';
    $('editDialog').showModal();await previewEditTime();
  }
  $('editTime').oninput=()=>{editPreviewSequence++;editPreviewPending=true;$('saveEdit').disabled=true;$('editOffset').textContent='';$('editBeijing').classList.remove('error');$('editBeijing').textContent='北京时间：换算中…';clearTimeout(editPreviewTimer);editPreviewTimer=setTimeout(previewEditTime,200);};
  $('editTime').onchange=()=>{clearTimeout(editPreviewTimer);previewEditTime();};
  $('closeEdit').onclick=()=>{if(!editBusy)$('editDialog').close();};
  $('editDialog').addEventListener('cancel',event=>{if(editBusy)event.preventDefault();});
  $('editForm').onsubmit=async event=>{
    event.preventDefault();if(editBusy||!editing||editPreviewPending)return;
    editBusy=true;$('saveEdit').disabled=true;$('closeEdit').disabled=true;$('editError').textContent='';
    try{
      const result=await api('edit',{id:editing.job.id,version:editing.version,text:$('editCaption').value,local_time:editing.can_edit_time&&$('editTime').value!==editing.originalTime?$('editTime').value:null});
      state.jobs=state.jobs.map(j=>j.id===result.id?result:j);renderJobs();$('editDialog').close();await loadJobs();
    }catch(err){$('editError').textContent=err.message;}
    finally{editBusy=false;$('saveEdit').disabled=editPreviewPending;$('closeEdit').disabled=false;}
  };
  function showReport(id, open=true) {
    const j=state.jobs.find(item=>item.id===id);if(!j)return;
    $('report').dataset.jobId=id;
    $('reportBody').innerHTML=`<div class="report-summary">${esc(j.channel_name)} · ${esc(names[j.state]||j.state)}\n${esc(j.text)}\n\n${j.due_at?'计划 '+esc(format(j.due_at,j.timezone))+'\n北京时间：'+esc(format(j.due_at,'Asia/Shanghai',false)):''}\n${j.post_id?'Buffer ID '+esc(j.post_id):'本地任务 '+esc(j.id)}</div>${j.state==='uncertain'?'<p class="error">请到 Buffer 核对结果，系统未自动重发。</p>':''}${j.external_link&&/^https:\/\//.test(j.external_link)?`<a class="report-link" href="${esc(j.external_link)}" target="_blank" rel="noopener noreferrer">查看已发布视频 ↗</a>`:''}${(j.logs||[]).map(l=>`<div class="log-entry"><time>${esc(format(l.at,j.timezone))}</time><span>${esc(l.message)}</span></div>`).join('')}`;
    if(open&&!$('report').open)$('report').showModal();
  }
  function upload(file) {
    return new Promise((resolve,reject)=>{
      const xhr=new XMLHttpRequest();xhr.open('POST','/api/publish/upload?name='+encodeURIComponent(file.name));xhr.setRequestHeader('Content-Type','video/mp4');xhr.timeout=600000;
      xhr.upload.onprogress=e=>{if(e.lengthComputable)$('uploadProgress').value=e.loaded/e.total*100;};
      xhr.onerror=()=>reject(new Error('上传连接中断，请重试'));xhr.ontimeout=()=>reject(new Error('上传超时，请重试'));
      xhr.onload=()=>{try{const data=JSON.parse(xhr.responseText);xhr.status<300?resolve(data):reject(new Error(data.error||'上传失败'));}catch{reject(new Error('上传结果无法确认'));}};
      xhr.send(file);
    });
  }
  $('videoFile').onchange=async()=>{
    const file=$('videoFile').files[0];if(!file)return;
    state.asset=null;state.busy=true;state.requestId=null;ready();$('composeError').textContent='';
    try{
      if(!/\.mp4$/i.test(file.name)||file.size>1024**3)throw new Error('请选择 1 GB 以内的 MP4 视频');
      $('fileName').textContent='正在上传…';$('uploadProgress').hidden=false;
      state.asset=await upload(file);$('fileName').textContent=state.asset.name;
      $('fileMeta').textContent=`${state.asset.width} × ${state.asset.height} · ${state.asset.duration} 秒 · ${(file.size/1024/1024).toFixed(1)} MB`;
    }catch(e){$('composeError').textContent=e.message;$('fileName').textContent='重新选择视频';}
    finally{state.busy=false;$('uploadProgress').hidden=true;ready();}
  };
  $('caption').oninput=()=>{$('count').textContent=$('caption').value.length+' / 2200';state.requestId=null;};
  $('channel').onchange=()=>{state.requestId=null;ready();};
  $('publishTime').oninput=()=>{state.requestId=null;previewSequence++;$('timezone').textContent='';$('beijingTime').textContent='北京时间：换算中…';clearTimeout(previewTimer);previewTimer=setTimeout(previewTime,200);};
  $('publishTime').onchange=()=>{state.requestId=null;clearTimeout(previewTimer);previewTime();};
  $('aiGenerated').onchange=() => state.requestId=null;
  document.querySelectorAll('[data-mode]').forEach(button=>button.onclick=()=>{
    state.mode=button.dataset.mode;state.requestId=null;document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('selected',b===button));
    $('scheduleField').hidden=state.mode!=='schedule';$('publishTime').required=state.mode==='schedule';
    $('submit').firstChild.textContent=({now:'立即发布',schedule:'创建定时任务',draft:'存入 Buffer 草稿'}[state.mode])+' ';
  });$('publishTime').required=true;
  document.querySelectorAll('[data-filter]').forEach(button=>button.onclick=()=>{state.filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button));renderJobs();});
  $('compose').onsubmit=async e=>{
    e.preventDefault();if(state.busy)return;
    state.busy=true;ready();$('composeError').textContent='';state.requestId ||= requestId();
    try{
      await api('jobs',{request_id:state.requestId,asset_id:state.asset.id,text:$('caption').value,channel_id:$('channel').value,mode:state.mode,local_time:$('publishTime').value,ai_generated:$('aiGenerated').checked});
      state.requestId=null;state.asset=null;$('caption').value='';$('count').textContent='0 / 2200';$('fileName').textContent='选择视频';$('fileMeta').textContent='MP4 · 3 秒–10 分钟 · 最大 1 GB';$('videoFile').value='';await loadJobs();
    }catch(err){$('composeError').textContent=err.message;}
    finally{state.busy=false;ready();}
  };
  $('jobs').onclick=async e=>{
    const b=e.target.closest('[data-action]');if(!b)return;
    if(b.dataset.action==='report')return showReport(b.dataset.id);
    b.disabled=true;$('listError').textContent='';
    try{if(b.dataset.action==='edit'){await openEdit(b.dataset.id);b.disabled=false;}else{await api(b.dataset.action,{id:b.dataset.id});await loadJobs();}}catch(err){$('listError').textContent=err.message;b.disabled=false;}
  };
  $('connect').onclick=()=>{$('bufferKey').value='';$('settings').showModal();};$('closeSettings').onclick=()=>$('settings').close();$('closeReport').onclick=()=>$('report').close();
  $('settingsForm').onsubmit=async e=>{e.preventDefault();$('saveKey').disabled=true;$('settingsError').textContent='';try{await api('config',{buffer_key:$('bufferKey').value});$('bufferKey').value='';await loadStatus();await loadChannels();$('settings').close();}catch(err){$('settingsError').textContent=err.message;}finally{$('saveKey').disabled=false;}};
  $('reloadChannels').onclick=()=>loadChannels().catch(e=>$('composeError').textContent=e.message);
  $('refreshJobs').onclick=()=>loadJobs().catch(e=>$('listError').textContent=e.message);
  Promise.all([loadStatus().then(loadChannels),loadJobs()]).catch(e=>$('composeError').textContent=e.message);
  setInterval(renderClock,1000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadStatus().catch(e=>$('composeError').textContent=e.message);});
  setInterval(()=>{if(!document.hidden)loadJobs().catch(e=>$('listError').textContent=e.message);},15000);
})();
