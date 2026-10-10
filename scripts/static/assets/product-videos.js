"use strict";
const $ = id => document.getElementById(id);
const state = {mode:"products", page:1, hasMore:false, librarySeq:0, products:[], selected:"", videos:[], job:null, loading:false, timer:null, seq:0, editing:null, imports:[], media:null, mediaState:null, playRequested:"", submitting:false};
Object.assign(state,{selecting:false,checked:new Set(),pageVideos:[],tables:[],writing:false});
state.reviews = new Map();state.savedReviews=[];state.review=null;state.reviewSeq=0;state.reviewTimer=null;
async function loadReviewLinks() {
  try {
    const files=await api('/api/files');
    const count=(Array.isArray(files)?files:[]).filter(f=>f.review_available).length;
    state.savedReviews=(Array.isArray(files)?files:[]).filter(f=>f.review_available);
    $("reviewLibraryCount").textContent=`（${count}）`;
    state.reviews=new Map((Array.isArray(files)?files:[]).filter(f=>f.review_available&&/^\d{15,25}$/.test(String(f.review_video_id||''))).map(f=>[String(f.review_video_id),f]));
    renderVideos();
  } catch { /* Reviews are optional; collection, playback and audio remain available. */ }
}
function reviewSnippet(video) {
  const review=state.reviews.get(String(video.video_id));
  const link=video.collection_link;
  const badge=link?`<p class="review-collection-link">${link.retention_available?'已关联采集留存':'已关联采集记录'} · ${esc(String(link.collected_at||'').slice(0,10))}</p>`:'';
  const inputs=`<button class="video-review-button" data-review-inputs="${esc(video.video_id)}">补充数据 · 留存 / 出单</button>`;
  if(!review)return badge+inputs;
  return badge+`<section class="video-review"><strong>复盘摘要</strong><p>${esc(review.review_summary||'已有复盘，可直接查看')}</p><small>依据 ${esc(String(review.review_collected_at||'未标注采集时间').slice(0,10))} 的历史指标。</small></section>`+inputs;
}
function videoStatus(video) {
  if(['queued','delayed','preparing','collecting','retrying'].includes(video.collection_status))return ['collecting','采集中'];
  if(state.reviews.has(String(video.video_id)))return ['reviewed','复盘完成'];
  if(video.collection_link?.retention_available)return ['collected','采集完成'];
  return video.downloaded?['cached','已缓存']:['uncached','未缓存'];
}
function updateVideoStatuses() {
  document.querySelectorAll('[data-video-status]').forEach(badge=>{
    const video=state.videos.find(v=>String(v.video_id)===badge.dataset.videoStatus);if(!video)return;
    const [status,label]=videoStatus(video);badge.dataset.status=status;badge.textContent=label;
  });
}
const tableCacheKey="video-table-targets-v1";
const videoTableNames=["视频数据表-鹏飞","视频数据表-丽娜","视频数据表-小周"];
let tableRequest=null;
try {const cached=JSON.parse(sessionStorage.getItem(tableCacheKey));if(Array.isArray(cached))state.tables=cached.filter(t=>videoTableNames.includes(t.appName)&&t.tableName==="数据表");}catch{}
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const fmt = value => value == null ? "—" : Number(value).toLocaleString("zh-CN");
const date = (value, full=false) => value ? new Date(value*1000).toLocaleString("zh-CN", full ? {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"} : {year:"numeric",month:"2-digit",day:"2-digit"}) : "—";
const busy = () => state.submitting || ["running","queued"].includes(state.job?.status);
const current = () => state.products.find(p => p.product_id === state.selected);
function endpoint(name, pid=state.selected, vid="") {return `/api/product-videos/${name}?`+new URLSearchParams({product_id:pid,...(vid?{video_id:vid}:{})});}
function url(value) {try {const u = new URL(value);return ["http:","https:"].includes(u.protocol)?u.href:"";}catch{return "";}}
function imageURL(p, v=null) {if(!v&&p.handle)return p.image_url;return endpoint("image",p.product_id,v?.video_id||"")+"&v="+encodeURIComponent(v?.cover_url || p.image_url || "");}
function image(p, v=null, lazy=true) {return (v?.cover_url || (!v && p.image_url)) ? `<img src="${esc(imageURL(p,v))}" alt="${v?"视频封面":"商品主图"}" ${lazy?'loading="lazy"':""} decoding="async">` : "▧";}
let toastTimer;
function toast(message, error=false) {$("toast").textContent=message;$("toast").className="toast"+(error?" error":"");$("toast").hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$("toast").hidden=true,6000);}
function inlineError(id, message="") {$(id).textContent=message;$(id).hidden=!message;}
async function api(path, body) {
  const response=await fetch(path,{...(body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(120000)});
  const data=await response.json();if(!response.ok)throw new Error(data.error||"请求失败，请重试");return data;
}
function renderProducts() {
  const q=$("productFilter").value.trim().toLowerCase();
  const products=state.products.filter(p=>(p.product_name+" "+p.product_id+" "+(p.handle||"")).toLowerCase().includes(q));
  $("productCount").textContent=state.products.length;
$("products").innerHTML=products.map(p=>`<button class="product-row ${p.product_id===state.selected?"active":""}" data-product="${esc(p.product_id)}" aria-pressed="${p.product_id===state.selected}"><span class="product-thumb">${image(p)}</span><span class="product-row-copy"><strong>${esc(p.product_name)}</strong><span class="row-meta"><span>${esc(p.price||"价格未知")}</span><span>${esc(p.status||"状态未知")}</span></span><span class="row-id">${esc(p.handle?(p.feishu_user_name||"未绑定飞书"):p.product_id)}</span></span></button>`).join("")||`<div class="empty small">${state.products.length?"没有匹配的结果":state.mode==="accounts"?"IP 池暂无 TikTok 账号，请到 IP 池添加":"商品库还是空的，点击 ＋ 添加商品"}</div>`;
}
async function loadProducts(preferred=state.selected) {
  const token=++state.librarySeq;const result=await api(state.mode==="accounts"?endpoint("accounts"):"/api/proxy/products");if(token!==state.librarySeq)return;state.products=result.products||[];renderProducts();
  const selected=state.products.find(p=>p.product_id===preferred)||state.products[0];
  if(selected)await selectProduct(selected.product_id);
  else {state.selected="";state.seq++;clearTimeout(state.timer);$("productWorkspace").hidden=true;$("workspaceEmpty").hidden=false;}
}
async function switchLibrary(mode) {
  if(mode===state.mode||state.submitting)return;
  state.mode=mode;state.selected="";state.products=[];state.videos=[];state.job=null;state.hasMore=false;state.seq++;clearTimeout(state.timer);
  const account=mode==="accounts";
  $("videoSort").value=account?"published_at":"views";
  $("productsTab").setAttribute("aria-pressed",String(!account));$("accountsTab").setAttribute("aria-pressed",String(account));
  $("libraryTitle").textContent=account?"账号池":"商品库";$("addProduct").hidden=account;
  $("productFilter").value="";$("productFilter").placeholder=account?"搜索账号名称或 @账号":"搜索商品名称或 ID";$("productFilter").setAttribute("aria-label",account?"搜索账号池":"搜索商品库");
  $("libraryMeta").textContent=account?"复用 IP 池账号 · 切换不扣额度":"与视频发布共用商品库";$("libraryFoot").textContent=account?"账号维护请前往 IP 池":"主图本地缓存 · 无需重复远程加载";
  $("productWorkspace").hidden=true;$("workspaceEmpty").hidden=false;renderProducts();await loadProducts();
}
function renderOverview() {
  const p=current();if(!p)return;
  $("workspaceEmpty").hidden=true;$("productWorkspace").hidden=false;
  $("selectedName").textContent=p.product_name;
  $("selectedImage").innerHTML=image(p,null,false);
  $("selectedMeta").textContent=p.handle?`@${p.handle} · 来自 IP 池`:`ID ${p.product_id} · ${p.price||"价格未知"} · 库存 ${p.stock||"未知"}`;
  $("editProduct").hidden=Boolean(p.handle);$("deleteProduct").hidden=Boolean(p.handle);$("shopLink").textContent=p.handle?"账号主页 ↗":"商品主页 ↗";
  $("sourceEyebrow").textContent=p.handle?"ACCOUNT VIDEO LIBRARY":"PRODUCT VIDEO LIBRARY";
  $("summaryNote").innerHTML=p.handle?"查询最新一页<br><span>历史视频手动加载，已有数据持续保留。</span>":"发现关联内容，持续更新表现。<br><span>接口返回部分关联视频，不代表全量。</span>";
  $("shopLink").href=url(p.product_url)||`https://www.tiktok.com/shop/pdp/${p.product_id}`;
}
async function selectProduct(pid) {
  state.checked.clear();
  clearTimeout(state.timer);state.seq++;state.selected=pid;state.page=1;state.videos=[];state.job=null;state.hasMore=false;state.loading=true;
  $("videoFilter").value="";renderProducts();renderOverview();renderVideos();await loadVideos();
}
async function loadVideos() {
  const pid=state.selected, seq=state.seq;if(!pid)return;
  try {
    const collecting=state.loading?[]:state.videos.filter(v=>videoStatus(v)[0]==='collecting');
    if(collecting.length) {
      const results=await Promise.all(collecting.map(v=>api(endpoint('review-inputs',pid,v.video_id))));if(seq!==state.seq)return;
      results.forEach((data,index)=>{collecting[index].collection_status=data.collection_job?.status;collecting[index].collection_link=data.collection_link;});
      updateVideoStatuses();
      if(state.videos.some(v=>videoStatus(v)[0]==='collecting')){clearTimeout(state.timer);state.timer=setTimeout(loadVideos,2500);return;}
    }
    if(!state.loading && state.job?.action==="review" && busy()) {
      const progress=await api(endpoint("review",pid,state.job.video_id));if(seq!==state.seq)return;
      if(["processing","waiting"].includes(progress.status)) {
        state.job.message=progress.message;renderJobStatus();
        clearTimeout(state.timer);state.timer=setTimeout(loadVideos,2500);return;
      }
    }
    const result=await api(endpoint("list",pid));if(seq!==state.seq)return;
    const keepCards=!state.loading && ['play','download','audio','review'].includes(result.job?.action) && ['running','queued'].includes(result.job?.status);
    state.videos=result.videos;state.job=result.job;state.hasMore=Boolean(result.has_more);state.loading=false;
    if(keepCards){renderJobStatus();updateVideoStatuses();}else renderVideos();
    if(state.media && $("mediaDialog").open)await renderMedia();
  } catch(e) {if(seq===state.seq){const initial=state.loading;state.loading=false;if(initial)renderVideos();toast(e.message,true);}}
  if(seq===state.seq && (busy()||state.videos.some(v=>videoStatus(v)[0]==='collecting'))){clearTimeout(state.timer);state.timer=setTimeout(loadVideos,2500);}
}
function card(v) {
  const p=current();const stats=[["views","播放"],["likes","点赞"],["comments","评论"],["shares","分享"],["saves","收藏"]];
  return `<article class="video-card"><button class="video-cover" data-media="${esc(v.video_id)}" aria-label="查看视频：${esc(v.title||v.video_id)}">${v.cover_url?image(p,v):'<span class="cover-empty">暂无封面</span>'}<span class="play-icon" aria-hidden="true">▷</span><span class="download-badge" data-video-status="${esc(v.video_id)}" data-status="${videoStatus(v)[0]}">${videoStatus(v)[1]}</span><span class="cover-meta"><span>${date(v.published_at)}</span><span>${Math.round(v.duration||0)}s</span></span></button><div class="video-body"><div class="video-author">${esc(v.author||"未知作者")}</div><h4 class="video-title">${esc(v.title||"无标题视频")}</h4><div class="video-stats">${stats.map(([key,label])=>`<div><b>${fmt(v[key])}</b><span>${label}</span></div>`).join("")}<div><b>${v.duration?Math.round(v.duration)+"s":"—"}</b><span>时长</span></div></div>${reviewSnippet(v)}<button class="video-review-button" data-review="${esc(v.video_id)}">内容复盘</button><div class="video-actions"><a href="${esc(url(v.url))}" target="_blank" rel="noopener noreferrer">原视频 ↗</a><button data-refresh="${esc(v.video_id)}" ${busy()?"disabled":""}>更新数据</button><button data-media="${esc(v.video_id)}">播放 / 音频</button></div><div class="video-time">${v.updated_at?"详细数据更新于 "+date(v.updated_at,true):"基础数据获取于 "+date(v.basic_updated_at,true)}</div>${v.error?`<div class="video-error">${esc(v.error)}</div>`:""}</div></article>`;
}
function renderSelection() {
  $("toggleSelection").textContent=state.selecting?"取消多选":"多选";
  $("toggleSelection").setAttribute("aria-pressed",String(state.selecting));
  for(const id of ["selectPageLabel","selectionCount","batchAction"])$(id).hidden=!state.selecting;
  $("selectionCount").textContent=`已选 ${state.checked.size} 条（含其他页）`;
  $("batchAction").disabled=!state.checked.size;
  const count=state.pageVideos.filter(v=>state.checked.has(v.video_id)).length;
  $("selectPage").checked=count>0&&count===state.pageVideos.length;
  $("selectPage").indeterminate=count>0&&count<state.pageVideos.length;
  $("selectPage").disabled=!state.pageVideos.length;
  document.querySelectorAll(".video-card").forEach((el,i)=>{
    const v=state.pageVideos[i];if(!v)return;
    el.classList.toggle("is-selected",state.checked.has(v.video_id));
    let label=el.querySelector(".video-select");
    if(!label&&state.selecting){label=document.createElement("label");label.className="video-select";label.innerHTML=`<input type="checkbox" data-select-video="${esc(v.video_id)}" aria-label="选择视频：${esc(v.title||v.video_id)}">选择`;el.prepend(label);}
    if(label){label.hidden=!state.selecting;label.querySelector("input").checked=state.checked.has(v.video_id);}
  });
}
function tableChoiceChanged() {
  const target=state.tables[$("tableChoice").value];
  $("confirmTable").disabled=state.writing||!target||!state.checked.size;
  $("tableFields").textContent=target?"表格支持字段："+(target.matched_fields||[]).join("、"):"";
  const link=url(target?.url);$("openTableLink").hidden=!link;
  if(link)$("openTableLink").href=link;else $("openTableLink").removeAttribute("href");
}
async function loadTables(refresh=false) {
  $("refreshTables").disabled=true;inlineError("tableError");
  try {
    if(refresh||!state.tables.length){
      $("tableChoice").innerHTML='<option value="">正在加载表格…</option>';tableChoiceChanged();
      if(!tableRequest)tableRequest=api(endpoint("tables")).then(data=>{
        state.tables=(data.targets||[]).filter(t=>videoTableNames.includes(t.appName)&&t.tableName==="数据表").sort((a,b)=>videoTableNames.indexOf(a.appName)-videoTableNames.indexOf(b.appName));
        try {sessionStorage.setItem(tableCacheKey,JSON.stringify(state.tables));}catch{}
      }).finally(()=>{tableRequest=null;});
      await tableRequest;
    }
    $("tableChoice").innerHTML='<option value="">请选择表格</option>'+state.tables.map((t,i)=>`<option value="${i}">${esc(t.appName)} / ${esc(t.tableName)}</option>`).join("");
    tableChoiceChanged();
    if(!state.tables.length)inlineError("tableError","暂无可用的视频数据表，请检查表格授权后刷新。");
  }catch(e){$("tableChoice").innerHTML='<option value="">表格加载失败</option>';tableChoiceChanged();inlineError("tableError",e.message);}
  finally{$("refreshTables").disabled=false;}
}
async function openTable() {
  if(!state.checked.size)return;
  $("tableSummary").textContent=`将写入已选的 ${state.checked.size} 条视频`;
  $("confirmTable").disabled=true;
  $("tableFields").textContent="";$("tableProgress").textContent="";inlineError("tableError");$("tableDialog").showModal();
  await loadTables();
}
async function writeTable() {
  const target=state.tables[$("tableChoice").value];if(state.writing||!target||!state.checked.size)return;
  state.writing=true;const ids=[...state.checked],pid=state.selected;let success=0;const failures=[];
  $("confirmTable").disabled=true;$("tableChoice").disabled=true;$("refreshTables").disabled=true;inlineError("tableError");
  document.querySelectorAll('[data-close="tableDialog"]').forEach(el=>el.disabled=true);
  try {for(const video_id of ids){
    $("tableProgress").textContent=`正在写入 ${success+failures.length+1} / ${ids.length}…`;
    try {await api("/api/product-videos/write-table",{product_id:pid,video_id,appToken:target.appToken,tableId:target.tableId});success++;state.checked.delete(video_id);}
    catch(e){failures.push(`${video_id}：${e.message}`);}
  }}finally{state.writing=false;$("tableChoice").disabled=false;$("refreshTables").disabled=false;document.querySelectorAll('[data-close="tableDialog"]').forEach(el=>el.disabled=false);renderSelection();}
  $("tableProgress").textContent=`写入完成：成功 ${success} 条，失败 ${failures.length} 条。`;
  inlineError("tableError",failures.join("\n"));$("confirmTable").disabled=!failures.length;
  $("tableSummary").textContent=failures.length?`仍选中 ${failures.length} 条未成功的视频，可核查后重试。`:"已选视频全部写入完成。";
}
function renderVideos() {
  const p=current();if(!p)return;
  $("refreshAll").disabled=busy()||state.loading;$("editProduct").disabled=busy();$("deleteProduct").disabled=busy();
  $("refreshAll").textContent=busy()?"↻ 正在处理…":p.handle?"↻ 查询最新一页":"↻ 更新全部视频";
  $("loadMore").hidden=!p.handle||!state.hasMore;$("loadMore").disabled=busy()||state.loading;
  $("productsTab").disabled=state.submitting;$("accountsTab").disabled=state.submitting;
  $("totalVideos").textContent=state.loading?"—":state.videos.length;
  for(const [key,id] of [["views","totalViews"],["likes","totalLikes"]]) {const known=state.videos.filter(v=>v[key]!=null);$(id).textContent=known.length?fmt(known.reduce((n,v)=>n+Number(v[key]),0)):"—";}
  renderJobStatus();
  const q=$("videoFilter").value.trim().toLowerCase(), key=$("videoSort").value;
  const visible=state.videos.filter(v=>(v.title+" "+v.author+" "+v.video_id).toLowerCase().includes(q)).sort((a,b)=>(b[key]??-1)-(a[key]??-1));
  $("visibleVideos").textContent=visible.length;
  const pages=Math.max(1,Math.ceil(visible.length/10));state.page=Math.min(state.page,pages);
  state.pageVideos=state.loading?[]:visible.slice((state.page-1)*10,state.page*10);
  $("videoPagination").hidden=state.loading||!visible.length;
  $("pageInfo").textContent=`第 ${state.page} / ${pages} 页 · 共 ${visible.length} 条`;
  $("previousPage").disabled=state.page===1;$("nextPage").disabled=state.page===pages;
  const latest=Math.max(0,...state.videos.map(v=>v.updated_at||v.basic_updated_at||0));
  $("lastUpdated").textContent=latest?`最近数据时间 ${date(latest,true)} · 点击更新获取最新指标` : "按需查询，不会自动消耗接口额度";
  const markup=state.loading?'<div class="empty">正在读取已保存的视频…</div>':visible.length?visible.slice((state.page-1)*10,state.page*10).map(card).join(""):state.videos.length?'<div class="empty">没有匹配的视频，试试其他关键词。</div>':busy()?'<div class="empty"><h3>正在查找关联视频…</h3><p>结果会自动显示，可以稍后回来查看。</p></div>':`<div class="empty"><h3>${state.job?"暂无已收录的视频":(p.handle?"还没有查询这个账号":"还没有查询这个商品")}</h3><p>${state.job?"本次没有返回关联视频，不代表没有带货内容。":(p.handle?"查询账号最新一页视频及指标。":"查询该商品的关联视频，并保存播放、点赞等表现数据。")}</p><button class="primary" data-refresh-all>${p.handle?"查询最新一页":"查询关联视频"}</button></div>`;
  if(state.videoMarkup!==markup){$("videos").innerHTML=markup;state.videoMarkup=markup;}
  renderSelection();
}
function renderJobStatus() {
  $("jobStatus").hidden=!state.job;$("jobStatus").className="job-status "+(state.job?.status||"");$("jobMessage").textContent=state.job?.message||"";
  $("jobProgress").hidden=!busy();if(state.job?.total){$("jobProgress").max=state.job.total;$("jobProgress").value=state.job.done;}else $("jobProgress").removeAttribute("value");
}
async function startJob(action="refresh", vid="") {
  if(busy())return;
  state.submitting=true;renderVideos();
  try {state.job=await api("/api/product-videos/jobs",{action,product_id:state.selected,video_id:vid});}
  finally {state.submitting=false;renderVideos();}
  await loadVideos();
}
function fillForm(p={}) {for(const name of ["product_id","product_name","product_url","image_url","price","stock","status"])$("productForm").elements[name].value=p[name]??(name==="status"?"Active":"");}
function openEditor(edit=false) {
  state.editing=edit?{...current()}:null;fillForm(state.editing||{});state.imports=[];
  $("editorTitle").textContent=edit?"编辑商品":"添加商品";$("importSection").hidden=edit;$("productForm").elements.product_id.readOnly=edit;
  $("importResults").innerHTML="";$("importQuery").value="";inlineError("editorError");$("editor").showModal();
}
async function searchImport() {
  const query=$("importQuery").value.trim();if(!query)return;
  $("searchImport").disabled=true;$("importResults").textContent="正在查找商品…";inlineError("editorError");
  try {const r=await api("/api/proxy/products/search",{query});state.imports=r.products||[];$("importResults").innerHTML=state.imports.map((p,i)=>`<button class="import-result" data-import="${i}" ${p.already_added?"disabled":""}>${esc(p.product_name)}<span>${esc(p.product_id)} · ${esc(p.price||"价格未知")} ${p.already_added?"· 已在商品库":"· 点击填入"}</span></button>`).join("")||"未找到商品";}
  catch(e){$("importResults").textContent="";inlineError("editorError",e.message);}finally{$("searchImport").disabled=false;}
}
async function saveProduct(event) {
  event.preventDefault();$("saveProduct").disabled=true;inlineError("editorError");
  const p={...(state.editing||{}),...Object.fromEntries(new FormData($("productForm"))),action:state.editing?"update":"create"};
  try {await api("/api/proxy/products",p);$("editor").close();await loadProducts(p.product_id);toast("商品已保存，共享商品库已同步");}
  catch(e){inlineError("editorError",e.message);}finally{$("saveProduct").disabled=false;}
}
function openDelete() {$("deleteName").textContent=current()?.product_name;inlineError("deleteError");$("deleteDialog").showModal();}
async function deleteProduct() {$("confirmDelete").disabled=true;try{await api("/api/proxy/products/delete",{product_id:state.selected});$("deleteDialog").close();await loadProducts();toast("商品已删除");}catch(e){inlineError("deleteError",e.message);}finally{$("confirmDelete").disabled=false;}}
async function openMedia(vid) {
  state.media=state.videos.find(v=>v.video_id===vid);state.mediaState=null;state.playRequested="";if(!state.media)return;
  $("mediaCaption").textContent=state.media.title||"无标题视频";$("mediaOriginal").href=url(state.media.url);
  $("player").innerHTML=image(current(),state.media,false)+'<button id="playVideo" class="play-overlay" aria-label="在线播放"><span>▷</span>在线播放</button>';$("audioPlayer").innerHTML="";$("downloadLinks").innerHTML="";
  $("originalText").textContent="提取音频后显示原文。";$("translatedText").textContent="识别完成后翻译为简体中文。";$("audioLanguage").textContent="";
  $("mediaDialog").showModal();await renderMedia();
}
async function renderMedia() {
  const video=state.media, pid=state.selected;if(!video||!$("mediaDialog").open)return;
  const media=await api(endpoint("media",pid,video.video_id));
  if(state.media?.video_id!==video.video_id||state.selected!==pid||!$("mediaDialog").open)return;
  const file=kind=>endpoint("file",pid,video.video_id)+"&kind="+kind;
  if(media.downloaded&&!state.mediaState?.downloaded){$("player").innerHTML=`<video controls playsinline preload="metadata" src="${esc(file("video"))}"></video>`;if(state.playRequested===video.video_id){state.playRequested="";$("player").querySelector("video").play().catch(()=>toast("视频已就绪，点击播放器即可播放"));}}
  if(!media.downloaded&&state.mediaState?.downloaded)$("player").innerHTML=image(current(),video,false)+'<button id="playVideo" class="play-overlay" aria-label="在线播放"><span>▷</span>在线播放</button>';
  if(media.audio_ready&&!state.mediaState?.audio_ready)$("audioPlayer").innerHTML=`<audio controls preload="metadata" src="${esc(file("audio"))}"></audio>`;
  $("downloadLinks").innerHTML=(media.audio_ready?`<a href="${esc(file("audio"))}&download=1" download>保存音频 ↓</a>`:"");
  $("originalText").textContent=media.transcript?.text||(media.transcript?"未识别到可翻译的语音。":"提取音频后显示原文。");
  $("translatedText").textContent=media.translation?.text||"识别完成后翻译为简体中文。";
  $("audioLanguage").textContent=media.transcript?.language||"";
  const related=state.job?.video_id===video.video_id&&!["refresh","more"].includes(state.job.action);
  $("mediaStatus").className="job-status "+(related?state.job.status:"");
  $("mediaStatus").textContent=related?state.job.message:busy()?"当前商品有任务正在处理，请稍候。":media.translation?"音频与翻译已保存，可直接查看。":media.downloaded?"视频已缓存，可直接播放或提取音频。":"点击封面在线播放，或直接提取音频并翻译。";
  if($("playVideo")){$("playVideo").disabled=busy();$("playVideo").innerHTML=busy()?"正在准备…":"<span>▷</span>在线播放";}
  $("extractAudio").disabled=busy()||Boolean(media.translation);$("extractAudio").textContent=media.translation?"✓ 翻译已完成":media.transcript?"继续翻译":"提取音频并翻译";
  state.mediaState=media;
}
function reviewText(value) {
  if(Array.isArray(value))return value.map(reviewText).join("；");
  if(value&&typeof value==="object")return Object.entries(value).map(([k,v])=>{
    const timeLabel={start:'开始',end:'结束',timestamp_seconds:'时间'}[k];
    return timeLabel&&typeof v==='number'?`${timeLabel}：${reviewSeconds(v)}秒`:`${k}：${reviewText(v)}`;
  }).join("\n");
  const evidence=state.review?.report?.['证据时间轴'];
  const readable=String(value??"").replace(/\b(r\d+|t\d+|frame_\d+)\b/g,id=>{
    const window=evidence?.retention_windows.find(w=>w.id===id);
    if(window)return `${reviewSeconds(window.start_seconds)}–${reviewSeconds(window.end_seconds)}秒`;
    const row=evidence?.timeline.find(r=>r.id===id);
    if(row)return `${reviewSeconds(row.start)}–${reviewSeconds(row.end)}秒`;
    const frame=evidence?.timeline.flatMap(r=>r.visuals).find(f=>f.frame_id===id);
    return frame?`${reviewSeconds(frame.seconds)}秒画面`:id;
  });
  return readable.replace(/(\d+(?:\.\d+)?)(\s*(?:–|—|-|→|至)\s*)(\d+(?:\.\d+)?)(\s*秒)/g,
    (_,start,separator,end)=>`${reviewSeconds(start)}–${reviewSeconds(end)}秒`)
    .replace(/\d+\.\d+(?=\s*秒)/g,reviewSeconds);
}
function reviewSeconds(value) {return String(Number(Number(value).toFixed(1)));}
function reviewFields(value) {
  const labels={play_count:'播放量',avg_watch_time:'平均观看时长',completion_rate:'完播率',可能原因:'可能原因 · 待验证'};
  return Object.entries(value||{}).map(([k,v])=>`<p><strong>${esc(labels[k]||k)}</strong><span>${esc(reviewText(v))}</span></p>`).join("");
}
function showReview(report, videoURL) {
  state.review.report=report;
  const source=report['采集数据来源']||{};
  const section=(title,items)=>Array.isArray(items)&&items.length?`<section><h3>${title}</h3>${items.map(item=>`<article class="native-review-card">${reviewFields(item)}</article>`).join("")}</section>`:"";
  $("nativeReviewResult").innerHTML=`<div class="native-review-summary"><h3>复盘要点</h3><p>${esc(reviewText(report.summary))}</p><button id="copyNativeReview">复制要点</button></div>${source.overview?`<details class="native-review-data"><summary>本次依据的表现数据 · ${esc(String(source.collected_at||'采集时间未知').slice(0,10))}</summary>${reviewFields(source.overview)}</details>`:""}${section('表现诊断',report['表现诊断'])}${section('优先调整',report['优先修改'])}<details><summary>内容与镜头细节</summary>${reviewFields(report['内容拆解'])}${section('原片镜头拆解',report['原片镜头拆解'])}</details><details><summary>数据范围与判断限制</summary><p>${esc(reviewText(report['数据限制']))}</p></details>`;
  if(videoURL)$("nativeReviewPreview").innerHTML=`<video controls playsinline preload="metadata" src="${esc(videoURL)}"></video>`;
  state.review.report=report;
  if(source.title)$("nativeReviewCaption").textContent=source.title;
  if(report['证据时间轴'])showEvidenceReview(report);
  else $("nativeReviewResult").insertAdjacentHTML('beforeend','<button id="regenerateNativeReview" class="primary">更新为时间轴复盘</button>');
  if(source.manual_orders)$("nativeReviewResult").insertAdjacentHTML('afterbegin',`<p class="job-status">人工补充：累计出单 ${esc(source.manual_orders.count)} 单 · 填写于 ${esc(source.manual_orders.recorded_at)}。与历史留存非同一快照。</p>`);
}
function evidenceRefs(ids) {
  const rows=state.review.report['证据时间轴'].timeline;
  return (ids||[]).map(id=>{const row=rows.find(r=>r.id===id);return row?`<button class="review-seek" data-review-seek="${row.start}">${reviewSeconds(row.start)}–${reviewSeconds(row.end)}秒 ↗</button>`:'';}).join(' ');
}
function showEvidenceReview(report) {
  const evidence=report['证据时间轴'],logic=report['视频逻辑'],rows=evidence.timeline;
  const chain=logic['推进'].map(beat=>`<article class="native-review-card"><button class="review-seek" data-review-seek="${beat.start}">依据 ${reviewSeconds(beat.start)}–${reviewSeconds(beat.end)}秒 ↗</button>${reviewFields({事件:beat['事件'],逻辑作用:beat['逻辑作用'],承接:beat['承接']})}</article>`).join('');
  const table=rows.map(row=>{
    const beats=logic['推进'].filter(b=>b['依据'].includes(row.id)).map(b=>({...b,逻辑作用:reviewText(b['逻辑作用'])}));
    const values=row.retention;
    const retention=values?`${values.start_percent}% → ${values.end_percent}%<br>${values.drop_percentage_points>0?'下降':values.drop_percentage_points<0?'回升':'变化'} ${reviewSeconds(Math.abs(values.drop_percentage_points))} 个百分点`:(row.start_percent!=null?`${row.start_percent}%（区间数据不完整）`:'未提供');
    const visuals=row.visuals.map(f=>`<p><small>${reviewSeconds(f.seconds)}秒</small> ${esc(f.text)}</p>`).join('')||'<p>此秒无采样帧，不补造画面。</p>';
    const unique=row.visuals.filter((f,i,a)=>i===0||f.text!==a[i-1].text);
    const preview=(unique.length>1?[unique[0],unique.at(-1)]:unique).map(f=>`<p><small>${reviewSeconds(f.seconds)}秒</small> ${esc(f.text.length>55?f.text.slice(0,55)+'…':f.text)}</p>`).join('')||'<p>此秒无采样帧</p>';
    return `<tr id="review-row-${row.id}"><td><button class="review-seek" data-review-seek="${row.start}">${reviewSeconds(row.start)}–${reviewSeconds(row.end)}秒 ↗</button></td><td>${preview}${row.speech.map(s=>`<p><small>语音原文${s.precision==='segment'?' · 整段引用':''}</small> ${esc(s.text)}</p>`).join('')}<details><summary>展开 ${row.visuals.length} 张采样证据</summary>${visuals}</details></td><td>${beats.map(b=>`<p>${esc(b['逻辑作用'])}</p>`).join('')||'未标注逻辑作用'}</td><td>${retention}</td></tr>`;
  }).join('');
  const contexts=ids=>(ids||[]).map(id=>{
    const row=rows.find(r=>r.id===id);if(!row)return '';
    return `<div class="review-context-row">${evidenceRefs([id])}<details><summary>查看画面与口播</summary>${row.visuals.map(f=>`<p>${reviewSeconds(f.seconds)}秒：${esc(f.text)}</p>`).join('')||'<p>无采样帧</p>'}${row.speech.map(s=>`<p><strong>${s.precision==='segment'?'整段引用 · '+reviewSeconds(s.start)+'–'+reviewSeconds(s.end)+'秒':'该秒语音原文'}</strong>${esc(s.text)}</p>`).join('')}</details></div>`;
  }).join('');
  const windows=evidence.retention_windows.map(w=>`<article class="native-review-card"><h4>${esc(w.label)} · ${w.start_seconds}–${w.end_seconds}秒</h4><p>${w.start_percent}% → ${w.end_percent}% · ${w.drop_percentage_points<0?'回升':'下降'} ${reviewSeconds(Math.abs(w.drop_percentage_points))} 个百分点</p><div class="review-context"><section><strong>变化前</strong>${contexts(w.before)||'<p>已在开头，无前段。</p>'}</section><section><strong>变化中</strong>${contexts(w.during)}</section><section><strong>变化后</strong>${contexts(w.after)||'<p>已到片尾。</p>'}</section></div></article>`).join('');
  const judgmentCard=j=>`<article class="native-review-card"><span class="review-judgment">${esc(j['判断'])}</span><h4>${esc(reviewText(j['要点']))}</h4><p>${esc(reviewText(j['解释']))}</p>${evidenceRefs(j['依据'])}</article>`;
  const judgments=report['逻辑合理性'].slice(0,4).map(judgmentCard).join('')+(report['逻辑合理性'].length>4?`<details><summary>更多待核实判断</summary>${report['逻辑合理性'].slice(4).map(judgmentCard).join('')}</details>`:'');
  const analyses=report['留存分析'].map(a=>{const w=evidence.retention_windows.find(w=>w.id===a['区间ID']);return `<article class="native-review-card"><h4>${esc(w?.label||'')} · ${w?.start_seconds}–${w?.end_seconds}秒</h4>${reviewFields({解释候选:a['解释候选'],其他解释:a['其他解释'],证据强度:a['证据强度']})}${evidenceRefs(a['依据'])}</article>`;}).join('');
  const source=report['采集数据来源']||{};
  $("nativeReviewResult").innerHTML=`<section><h3>1. 视频逻辑与统一时间轴</h3>${reviewFields({核心表达:logic['核心表达'],主体与道具:logic['主体与道具']})}<details open><summary>剧情与信息推进</summary>${chain}</details><details open><summary>逐秒证据与留存 · 点击时间回看</summary><div class="review-table-scroll"><table class="review-timeline"><thead><tr><th>时间</th><th>画面与口播事实</th><th>逻辑作用 · 模型理解</th><th>留存</th></tr></thead><tbody>${table}</tbody></table></div></details></section><section><h3>2. 留存变化与画面前后文</h3><p class="review-note">前后文是时间关系，不代表已经证明因果。相对平稳也不代表内容优秀。</p>${windows||'<div class="job-status">未提供有效逐秒留存，无法对齐变化区间。播放和互动不替代留存。</div>'}</section><section><h3>3. 逻辑合理性判断</h3>${judgments}</section><section><h3>4. 留存分析</h3>${analyses||'<p>缺少留存数据，本次不作留存归因。</p>'}</section><details><summary>可尝试的方向 · 由你判断</summary>${report['可尝试方向'].map(d=>`<article class="native-review-card">${reviewFields(d)}</article>`).join('')||'暂无充分依据提出调整方向。'}</details><details><summary>数据与判断范围</summary><p>采集时间：${esc(source.collected_at||'未知')}</p>${reviewFields(source.overview)}<p>${esc(reviewText(report['数据限制']))}</p><p>${esc(reviewText(logic['待核实']))}</p></details><details><summary>补充或纠正视频逻辑</summary><p class="review-note">你的解释会单独保存，重新分析会复用已提取的视频证据。</p><label for="nativeLogicNote">主推商品、剧情含义或需要纠正的理解</label><textarea id="nativeLogicNote" maxlength="4000" rows="4">${esc(report['人工补充']||'')}</textarea><button id="regenerateNativeReview" class="primary">按补充重新分析</button><p id="nativeCorrectionStatus" role="status"></p></details><button id="copyNativeReview">复制复盘要点</button>`;
  $("nativeReviewResult").querySelector('details').open=false;
  $("nativeReviewResult").querySelectorAll('details').forEach(details=>{
    if(details.querySelector(':scope > summary')?.textContent==='可尝试的方向 · 由你判断'){
      details.innerHTML='<summary>可尝试的方向 · 由你判断</summary>'+report['可尝试方向'].map(d=>`<article class="native-review-card">${reviewFields({方向:d['方向'],保留:d['保留'],验证:d['验证'],限制:d['限制']})}${evidenceRefs(d['依据'])}</article>`).join('');
    }
  });
  const result=$("nativeReviewResult"),detail=document.createElement('details');
  detail.className='review-deep-dive';detail.innerHTML='<summary>展开完整复盘与逐秒证据</summary>';
  [...result.children].filter(node=>node.id!=='copyNativeReview').forEach(node=>detail.appendChild(node));
  result.prepend(detail);
  const main=[...evidence.retention_windows].filter(w=>w.drop_percentage_points>0).sort((a,b)=>b.drop_percentage_points-a.drop_percentage_points)[0];
  result.insertAdjacentHTML('afterbegin',`<section class="native-review-summary"><h3>复盘重点</h3><p>${esc(reviewText(report.summary))}</p>${main?`<p class="review-note">数据依据：${reviewSeconds(main.start_seconds)}–${reviewSeconds(main.end_seconds)}秒，留存 ${main.start_percent}% → ${main.end_percent}%（下降 ${reviewSeconds(main.drop_percentage_points)} 个百分点）。</p>${evidenceRefs(main.during)}`:''}</section>`);
  result.querySelector('.native-review-summary').insertAdjacentHTML('beforeend','<p class="review-note">流失解释与调整方向仍需对照验证。</p>');
}
async function reviseReview() {
  const ref=state.review,note=$("nativeLogicNote")?.value.trim()||'';
  let pid=ref.pid,vid=ref.vid;
  if(!pid&&ref.filename){vid=state.savedReviews.find(f=>f.name===ref.filename)?.review_video_id;pid=state.selected;}
  if(!pid||!vid)throw new Error('请从所属账号或商品的视频卡片打开，再补充逻辑。');
  const token=state.reviewSeq;$("regenerateNativeReview").disabled=true;
  try {
    const job=await api('/api/product-videos/jobs',{action:'review',product_id:pid,video_id:String(vid),force:true,logic_note:note});
    if(token!==state.reviewSeq)return;
    if(job.action!=='review'||job.video_id!==String(vid)){toast('当前有其他任务，请完成后再提交补充。');return;}
    state.review={pid,vid:String(vid)};$("nativeReviewResult").innerHTML='';await pollReview(token);
  }finally{if($("regenerateNativeReview"))$("regenerateNativeReview").disabled=false;}
}
function reviewFailure(error) {$("nativeReviewStatus").textContent=error.message||"暂时无法读取复盘，请重试。";$("nativeReviewRetry").hidden=false;}
async function copyReview() {
  const report=state.review.report;
  const text=reviewText(report['证据时间轴']?{复盘要点:report.summary,视频逻辑:report['视频逻辑'],逻辑合理性:report['逻辑合理性'],留存数据:report['证据时间轴'].retention_windows,留存分析:report['留存分析'],可尝试方向:report['可尝试方向']}: {复盘要点:report.summary,表现诊断:report['表现诊断'],优先调整:report['优先修改']});
  if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(text);
  else {const field=document.createElement('textarea');field.value=text;$("reviewDialog").appendChild(field);field.select();const copied=document.execCommand('copy');field.remove();if(!copied)throw new Error('复制失败，请选中复盘文字复制。');}
  toast("复盘要点已复制");
}
async function pollReview(token, start=false) {
  const ref=state.review;if(!ref||token!==state.reviewSeq||!$("reviewDialog").open)return;
  try {
    const result=await api(endpoint("review",ref.pid,ref.vid));
    if(token!==state.reviewSeq||!$("reviewDialog").open)return;
    $("nativeReviewStatus").textContent=result.message;$("nativeReviewRetry").hidden=true;
    if(result.status==="ready"){showReview(result.report,result.video_url);loadReviewLinks();return;}
    if(result.status==="failed"&&!start){$("nativeReviewRetry").hidden=false;return;}
    if(["missing","failed"].includes(result.status)) {
      await api("/api/product-videos/jobs",{action:"review",product_id:ref.pid,video_id:ref.vid});
      if(token!==state.reviewSeq)return;
      $("nativeReviewStatus").textContent="正在生成复盘，关闭窗口后仍会继续。";
    }
    state.reviewTimer=setTimeout(()=>pollReview(token),2500);
  } catch(error){if(token===state.reviewSeq)reviewFailure(error);}
}
function prepareReview(caption) {
  clearTimeout(state.reviewTimer);state.reviewSeq++;
  $("nativeReviewCaption").textContent=caption;$("nativeReviewResult").innerHTML="";$("nativeReviewPreview").innerHTML="";
  $("nativeReviewRetry").hidden=true;$("nativeReviewStatus").textContent="正在读取复盘…";
  if($("mediaDialog").open)$("mediaDialog").close();
  if(!$("reviewDialog").open)$("reviewDialog").showModal();
}
async function openReviewInputs(vid) {
  const video=state.videos.find(v=>String(v.video_id)===String(vid));if(!video)return;
  clearTimeout(state.inputsTimer);
  const ref=state.inputs={pid:state.selected,vid:String(vid),allowed:false,savedCount:"",busy:false};
  $("reviewInputsCaption").textContent=video.title||vid;
  $("manualOrders").value="";$("manualOrdersTime").textContent="";
  $("reviewInputsHint").textContent="正在读取补充数据…";
  $("reviewCollectionStatus").textContent="";
  $("collectReviewRetention").disabled=true;
  $("saveManualOrders").disabled=true;$("reviewAfterInputs").disabled=true;
  $("reviewInputsDialog").showModal();
  const data=await api(endpoint('review-inputs',ref.pid,ref.vid));if(state.inputs!==ref)return;
  ref.savedCount=data.manual_orders?.count==null?"":String(data.manual_orders.count);
  $("manualOrders").value=ref.savedCount;
  $("saveManualOrders").disabled=false;
  $("manualOrdersTime").textContent=data.manual_orders?`保存于 ${data.manual_orders.recorded_at.slice(0,16).replace('T',' ')} UTC · 人工累计数据`:"尚未补充";
  $("reviewCollectionAccount").textContent=data.collection_allowed?`使用 @${data.collection_account} 绑定的 IP，仅采集当前视频并保存到本地。`:data.collection_error;
  $("reviewInputsHint").textContent="两项都可选；保存后复盘会使用最新补充。";
  updateReviewCollection(data,ref);
}
function updateReviewCollection(data,ref) {
  const job=data.collection_job;
  ref.allowed=!!data.collection_allowed;
  ref.active=!!job&&['queued','delayed','preparing','collecting','retrying'].includes(job.status);
  const link=data.collection_link;
  const video=state.videos.find(v=>String(v.video_id)===ref.vid);
  if(video){video.collection_status=job?.status;video.collection_link=link;updateVideoStatuses();}
  if(ref.active){clearTimeout(state.timer);state.timer=setTimeout(loadVideos,2500);}
  $("reviewCollectionStatus").textContent=job?`${job.status_label} · ${job.completed_videos}/${job.total_videos||'?'}${job.last_error?' · '+job.last_error:job.status_detail?' · '+job.status_detail:''}`:link?`${link.retention_available?'已有留存':'已有采集记录，留存不完整'} · ${String(link.collected_at||'').slice(0,10)}`:"尚未采集留存";
  $("collectReviewRetention").disabled=ref.active||!ref.allowed||ref.busy;
  $("reviewAfterInputs").disabled=ref.active||ref.busy;
  if(ref.active){
    clearTimeout(state.inputsTimer);
    state.inputsTimer=setTimeout(async()=>{
      if(state.inputs!==ref||!$("reviewInputsDialog").open)return;
      try{const next=await api(endpoint('review-inputs',ref.pid,ref.vid));if(state.inputs===ref)updateReviewCollection(next,ref);}catch(error){if(state.inputs===ref)$("reviewInputsHint").textContent=error.message;}
    },3000);
  }
}
async function saveReviewOrders() {
  const ref=state.inputs;if(!ref)return;
  if(ref.busy)throw new Error('正在保存或提交，请稍候');
  const value=$("manualOrders").value.trim();
  if(value&&!/^\d+$/.test(value))throw new Error('出单数量需为非负整数');
  ref.busy=true;$("saveManualOrders").disabled=true;$("reviewAfterInputs").disabled=true;
  try {
    const result=await api('/api/product-videos/orders',{product_id:ref.pid,video_id:ref.vid,count:value===''?null:Number(value)});
    if(state.inputs!==ref)return;
    ref.savedCount=value;
    $("manualOrdersTime").textContent=result.manual_orders?`已保存累计 ${result.manual_orders.count} 单 · 人工填写`:"已清除，出单数量未知";
    $("reviewInputsHint").textContent="已保存，下次复盘将使用本次补充。";
  } finally {
    ref.busy=false;
    if(state.inputs===ref){$("saveManualOrders").disabled=false;$("reviewAfterInputs").disabled=ref.active;}
  }
}
async function collectReviewRetention() {
  const ref=state.inputs;if(!ref||ref.busy||ref.active)return;
  if(!ref.allowed)throw new Error('视频所属账号尚未满足采集条件');
  ref.busy=true;$("collectReviewRetention").disabled=true;$("reviewAfterInputs").disabled=true;
  try {
    await api('/api/product-videos/collect-retention',{product_id:ref.pid,video_id:ref.vid});
    if(state.inputs!==ref)return;
    $("reviewInputsHint").textContent="已加入 Proxy 采集队列；完成后可生成复盘，关闭窗口也会继续。";
    const data=await api(endpoint('review-inputs',ref.pid,ref.vid));ref.busy=false;if(state.inputs===ref)updateReviewCollection(data,ref);
  } finally {ref.busy=false;if(state.inputs===ref){$("collectReviewRetention").disabled=ref.active||!ref.allowed;$("reviewAfterInputs").disabled=ref.active;}}
}
$("reviewInputsDialog").addEventListener('close',()=>{clearTimeout(state.inputsTimer);state.inputs=null;});
async function openReview(vid) {
  const video=state.videos.find(v=>String(v.video_id)===String(vid));if(!video)return;
  prepareReview(video.title||"无标题视频");state.review={pid:state.selected,vid:String(vid)};
  $("nativeReviewSaved").hidden=true;
  $("nativeReviewPreview").innerHTML=image(current(),video,false);
  await pollReview(state.reviewSeq);
}
async function openSavedReview(name) {
  if(!state.savedReviews.length){toast("暂无已生成的复盘，请点击视频卡片上的内容复盘。");return;}
  const file=state.savedReviews.find(f=>f.name===name)||state.savedReviews[0];
  prepareReview(file.review_summary||"已有视频复盘");state.review={filename:file.name};const token=state.reviewSeq;
  $("nativeReviewSaved").hidden=false;$("nativeReviewSaved").innerHTML=state.savedReviews.map((f,i)=>`<option value="${esc(f.name)}">视频 ${i+1} · ${esc(f.review_summary||f.name)}</option>`).join("");$("nativeReviewSaved").value=file.name;
  try {const result=await api('/api/result?filename='+encodeURIComponent(file.name));if(token!==state.reviewSeq)return;
    const report=result.assisted_review||result.audit_result||result.audit_result_zh;if(!report)throw new Error("复盘结果暂时不可用，请重试。");
    showReview(report,'/video/'+encodeURIComponent(file.name));$("nativeReviewStatus").textContent="复盘已完成 · 数据来自采集时的历史快照。";
  }catch(error){if(token===state.reviewSeq)reviewFailure(error);}
}
$("nativeReviewSaved").addEventListener("change",()=>openSavedReview($("nativeReviewSaved").value));
$("reviewDialog").addEventListener("close",()=>{clearTimeout(state.reviewTimer);state.reviewSeq++;$("nativeReviewPreview").querySelectorAll("video").forEach(v=>v.pause());state.review=null;});
document.querySelector("#mediaDialog .media-actions").insertAdjacentHTML("afterbegin",'<button id="mediaReviewButton" class="primary">内容复盘</button>');
document.addEventListener("error",event=>{if(event.target.tagName==="IMG"){event.target.replaceWith(Object.assign(document.createElement("span"),{textContent:"暂无图片"}));}},true);
document.addEventListener("click",event=>{
  const target=event.target.closest("button");if(!target||target.disabled)return;
  const run=async()=>{
    if(target.hasAttribute('data-review-seek')){
      const video=$("nativeReviewPreview").querySelector('video');if(!video)throw new Error('视频暂不可播放');
      const seconds=Number(target.dataset.reviewSeek);const seek=()=>{video.currentTime=seconds;video.play().catch(()=>toast('已定位画面，点击播放即可。'));};
      if(video.readyState>=1)seek();else video.addEventListener('loadedmetadata',seek,{once:true});
      $("nativeReviewPreview").scrollIntoView({block:'nearest'});return;
    }
    if(target.id==='regenerateNativeReview')return reviseReview();
    if(target.dataset.reviewInputs)return openReviewInputs(target.dataset.reviewInputs);
    if(target.id==='saveManualOrders')return saveReviewOrders();
    if(target.id==='collectReviewRetention')return collectReviewRetention();
    if(target.id==='reviewAfterInputs'){
      const ref=state.inputs;if(!ref)return;
      if($("manualOrders").value.trim()!==ref.savedCount)await saveReviewOrders();
      $("reviewInputsDialog").close();await loadVideos();return openReview(ref.vid);
    }
    if(target.dataset.review)return openReview(target.dataset.review);
    if(target.id==="mediaReviewButton")return openReview(state.media.video_id);
    if(target.id==="reviewLibraryLink")return openSavedReview();
    if(target.id==="nativeReviewRetry"){target.hidden=true;return state.review.filename?openSavedReview(state.review.filename):pollReview(state.reviewSeq,true);}
    if(target.id==="copyNativeReview")return copyReview();
    if(target.id==="toggleSelection"){state.selecting=!state.selecting;state.checked.clear();renderSelection();return;}
    if(target.id==="refreshTables")return loadTables(true);
    if(target.id==="confirmTable")return writeTable();
    if(target.dataset.close){$(target.dataset.close).close();return;}
    if(target.dataset.library)return switchLibrary(target.dataset.library);
    if(target.dataset.page){state.page+=Number(target.dataset.page);renderVideos();$("videos").scrollIntoView({block:"start"});return;}
    if(target.dataset.product)return selectProduct(target.dataset.product);
    if(target.dataset.media)return openMedia(target.dataset.media);
    if(target.dataset.refresh)return startJob("refresh",target.dataset.refresh);
    if(target.hasAttribute("data-refresh-all"))return startJob();
    if(target.dataset.import!=null){fillForm(state.imports[Number(target.dataset.import)]);$("productForm").elements.product_name.focus();return;}
    switch(target.id){case "loadMore":return startJob("more");case "addProduct":return openEditor();case "editProduct":return openEditor(true);case "reloadProducts":return loadProducts();case "refreshAll":return startJob();case "searchImport":return searchImport();case "deleteProduct":return openDelete();case "confirmDelete":return deleteProduct();case "playVideo":state.playRequested=state.media.video_id;return startJob("play",state.media.video_id);case "extractAudio":return startJob("audio",state.media.video_id);}
  };run().catch(e=>toast(e.message,true));
});
$("productFilter").addEventListener("input",renderProducts);$("videoFilter").addEventListener("input",()=>{state.page=1;renderVideos();});$("videoSort").addEventListener("change",()=>{state.page=1;renderVideos();});$("productForm").addEventListener("submit",saveProduct);
$("importQuery").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();searchImport();}});
$("mediaDialog").addEventListener("close",()=>{document.querySelectorAll("#mediaDialog video,#mediaDialog audio").forEach(el=>el.pause());state.media=null;});
$("videos").addEventListener("change",event=>{const id=event.target.dataset.selectVideo;if(id){if(event.target.checked)state.checked.add(id);else state.checked.delete(id);renderSelection();}});
$("selectPage").addEventListener("change",event=>{for(const v of state.pageVideos){if(event.target.checked)state.checked.add(v.video_id);else state.checked.delete(v.video_id);}renderSelection();});
$("batchAction").addEventListener("change",()=>{if($("batchAction").value==="write")openTable();$("batchAction").value="";});
$("tableChoice").addEventListener("change",tableChoiceChanged);
$("tableDialog").addEventListener("cancel",event=>{if(state.writing)event.preventDefault();});
switchLibrary("accounts").catch(e=>{$("products").innerHTML='<div class="empty small">账号池加载失败，请点击刷新列表重试。</div>';toast(e.message,true);});
loadReviewLinks();
$("reloadProducts").addEventListener("click", loadReviewLinks);
