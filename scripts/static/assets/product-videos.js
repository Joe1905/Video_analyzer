"use strict";
const $ = id => document.getElementById(id);
const state = {products:[], selected:"", videos:[], job:null, loading:false, timer:null, seq:0, editing:null, imports:[], media:null, mediaState:null, playRequested:"", submitting:false};
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const fmt = value => value == null ? "—" : Number(value).toLocaleString("zh-CN");
const date = (value, full=false) => value ? new Date(value*1000).toLocaleString("zh-CN", full ? {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"} : {year:"numeric",month:"2-digit",day:"2-digit"}) : "—";
const busy = () => state.submitting || ["running","queued"].includes(state.job?.status);
const current = () => state.products.find(p => p.product_id === state.selected);
function endpoint(name, pid=state.selected, vid="") {return `/api/product-videos/${name}?`+new URLSearchParams({product_id:pid,...(vid?{video_id:vid}:{})});}
function url(value) {try {const u = new URL(value);return ["http:","https:"].includes(u.protocol)?u.href:"";}catch{return "";}}
function imageURL(p, v=null) {return endpoint("image",p.product_id,v?.video_id||"")+"&v="+encodeURIComponent(v?.cover_url || p.image_url || "");}
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
  const products=state.products.filter(p=>(p.product_name+" "+p.product_id).toLowerCase().includes(q));
  $("productCount").textContent=state.products.length;
  $("products").innerHTML=products.map(p=>`<button class="product-row ${p.product_id===state.selected?"active":""}" data-product="${esc(p.product_id)}" aria-pressed="${p.product_id===state.selected}"><span class="product-thumb">${image(p)}</span><span class="product-row-copy"><strong>${esc(p.product_name)}</strong><span class="row-meta"><span>${esc(p.price||"价格未知")}</span><span>${esc(p.status||"状态未知")}</span></span><span class="row-id">${esc(p.product_id)}</span></span></button>`).join("")||`<div class="empty small">${state.products.length?"没有匹配的商品":"商品库还是空的，点击 ＋ 添加商品"}</div>`;
}
async function loadProducts(preferred=state.selected) {
  const result=await api("/api/proxy/products");state.products=result.products||[];renderProducts();
  const selected=state.products.find(p=>p.product_id===preferred)||state.products[0];
  if(selected)await selectProduct(selected.product_id);
  else {state.selected="";state.seq++;clearTimeout(state.timer);$("productWorkspace").hidden=true;$("workspaceEmpty").hidden=false;}
}
function renderOverview() {
  const p=current();if(!p)return;
  $("workspaceEmpty").hidden=true;$("productWorkspace").hidden=false;
  $("selectedName").textContent=p.product_name;
  $("selectedImage").innerHTML=image(p,null,false);
  $("selectedMeta").textContent=`ID ${p.product_id} · ${p.price||"价格未知"} · 库存 ${p.stock||"未知"}`;
  $("shopLink").href=url(p.product_url)||`https://www.tiktok.com/shop/pdp/${p.product_id}`;
}
async function selectProduct(pid) {
  clearTimeout(state.timer);state.seq++;state.selected=pid;state.videos=[];state.job=null;state.loading=true;
  $("videoFilter").value="";renderProducts();renderOverview();renderVideos();await loadVideos();
}
async function loadVideos() {
  const pid=state.selected, seq=state.seq;if(!pid)return;
  try {
    const result=await api(endpoint("list",pid));if(seq!==state.seq)return;
    state.videos=result.videos;state.job=result.job;state.loading=false;renderVideos();
    if(state.media && $("mediaDialog").open)await renderMedia();
  } catch(e) {if(seq===state.seq){state.loading=false;renderVideos();toast(e.message,true);}}
  if(seq===state.seq && busy()){clearTimeout(state.timer);state.timer=setTimeout(loadVideos,2500);}
}
function card(v) {
  const p=current();const stats=[["views","播放"],["likes","点赞"],["comments","评论"],["shares","分享"],["saves","收藏"]];
  return `<article class="video-card"><button class="video-cover" data-media="${esc(v.video_id)}" aria-label="查看视频：${esc(v.title||v.video_id)}">${v.cover_url?image(p,v):'<span class="cover-empty">暂无封面</span>'}<span class="play-icon" aria-hidden="true">▷</span>${v.downloaded?'<span class="download-badge">已缓存</span>':""}<span class="cover-meta"><span>${date(v.published_at)}</span><span>${Math.round(v.duration||0)}s</span></span></button><div class="video-body"><div class="video-author">${esc(v.author||"未知作者")}</div><h4 class="video-title">${esc(v.title||"无标题视频")}</h4><div class="video-stats">${stats.map(([key,label])=>`<div><b>${fmt(v[key])}</b><span>${label}</span></div>`).join("")}<div><b>${v.duration?Math.round(v.duration)+"s":"—"}</b><span>时长</span></div></div><div class="video-actions"><a href="${esc(url(v.url))}" target="_blank" rel="noopener noreferrer">原视频 ↗</a><button data-refresh="${esc(v.video_id)}" ${busy()?"disabled":""}>更新数据</button><button data-media="${esc(v.video_id)}">播放 / 音频</button></div><div class="video-time">${v.updated_at?"详细数据更新于 "+date(v.updated_at,true):"基础数据获取于 "+date(v.basic_updated_at,true)}</div>${v.error?`<div class="video-error">${esc(v.error)}</div>`:""}</div></article>`;
}
function renderVideos() {
  const p=current();if(!p)return;
  $("refreshAll").disabled=busy()||state.loading;$("editProduct").disabled=busy();$("deleteProduct").disabled=busy();
  $("refreshAll").textContent=busy()?"↻ 正在处理…":"↻ 更新全部视频";
  $("totalVideos").textContent=state.loading?"—":state.videos.length;
  for(const [key,id] of [["views","totalViews"],["likes","totalLikes"]]) {const known=state.videos.filter(v=>v[key]!=null);$(id).textContent=known.length?fmt(known.reduce((n,v)=>n+Number(v[key]),0)):"—";}
  $("jobStatus").hidden=!state.job;$("jobStatus").className="job-status "+(state.job?.status||"");$("jobMessage").textContent=state.job?.message||"";
  $("jobProgress").hidden=!busy();if(state.job?.total){$("jobProgress").max=state.job.total;$("jobProgress").value=state.job.done;}else $("jobProgress").removeAttribute("value");
  const q=$("videoFilter").value.trim().toLowerCase(), key=$("videoSort").value;
  const visible=state.videos.filter(v=>(v.title+" "+v.author+" "+v.video_id).toLowerCase().includes(q)).sort((a,b)=>(b[key]??-1)-(a[key]??-1));
  $("visibleVideos").textContent=visible.length;
  const latest=Math.max(0,...state.videos.map(v=>v.updated_at||v.basic_updated_at||0));
  $("lastUpdated").textContent=latest?`最近数据时间 ${date(latest,true)} · 点击更新获取最新指标` : "按需查询，不会自动消耗接口额度";
  const markup=state.loading?'<div class="empty">正在读取已保存的视频…</div>':visible.length?visible.map(card).join(""):state.videos.length?'<div class="empty">没有匹配的视频，试试其他关键词。</div>':busy()?'<div class="empty"><h3>正在查找关联视频…</h3><p>结果会自动显示，可以稍后回来查看。</p></div>':`<div class="empty"><h3>${state.job?"暂无已收录的视频":"还没有查询这个商品"}</h3><p>${state.job?"本次没有返回关联视频，不代表没有带货内容。":"查询该商品的关联视频，并保存播放、点赞等表现数据。"}</p><button class="primary" data-refresh-all>查询关联视频</button></div>`;
  if($("videos").innerHTML!==markup)$("videos").innerHTML=markup;
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
  const related=state.job?.video_id===video.video_id&&state.job.action!=="refresh";
  $("mediaStatus").className="job-status "+(related?state.job.status:"");
  $("mediaStatus").textContent=related?state.job.message:busy()?"当前商品有任务正在处理，请稍候。":media.translation?"音频与翻译已保存，可直接查看。":media.downloaded?"视频已缓存，可直接播放或提取音频。":"点击封面在线播放，或直接提取音频并翻译。";
  if($("playVideo")){$("playVideo").disabled=busy();$("playVideo").innerHTML=busy()?"正在准备…":"<span>▷</span>在线播放";}
  $("extractAudio").disabled=busy()||Boolean(media.translation);$("extractAudio").textContent=media.translation?"✓ 翻译已完成":media.transcript?"继续翻译":"提取音频并翻译";
  state.mediaState=media;
}
document.addEventListener("error",event=>{if(event.target.tagName==="IMG"){event.target.replaceWith(Object.assign(document.createElement("span"),{textContent:"暂无图片"}));}},true);
document.addEventListener("click",event=>{
  const target=event.target.closest("button");if(!target||target.disabled)return;
  const run=async()=>{
    if(target.dataset.close){$(target.dataset.close).close();return;}
    if(target.dataset.product)return selectProduct(target.dataset.product);
    if(target.dataset.media)return openMedia(target.dataset.media);
    if(target.dataset.refresh)return startJob("refresh",target.dataset.refresh);
    if(target.hasAttribute("data-refresh-all"))return startJob();
    if(target.dataset.import!=null){fillForm(state.imports[Number(target.dataset.import)]);$("productForm").elements.product_name.focus();return;}
    switch(target.id){case "addProduct":return openEditor();case "editProduct":return openEditor(true);case "reloadProducts":return loadProducts();case "refreshAll":return startJob();case "searchImport":return searchImport();case "deleteProduct":return openDelete();case "confirmDelete":return deleteProduct();case "playVideo":state.playRequested=state.media.video_id;return startJob("play",state.media.video_id);case "extractAudio":return startJob("audio",state.media.video_id);}
  };run().catch(e=>toast(e.message,true));
});
$("productFilter").addEventListener("input",renderProducts);$("videoFilter").addEventListener("input",renderVideos);$("videoSort").addEventListener("change",renderVideos);$("productForm").addEventListener("submit",saveProduct);
$("importQuery").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();searchImport();}});
$("mediaDialog").addEventListener("close",()=>{document.querySelectorAll("#mediaDialog video,#mediaDialog audio").forEach(el=>el.pause());state.media=null;});
loadProducts().catch(e=>{$("products").innerHTML='<div class="empty small">商品库加载失败，请点击刷新列表重试。</div>';toast(e.message,true);});
