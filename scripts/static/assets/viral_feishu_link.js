/* Read the table configured for this environment, never hardcode another port's table. */
(async()=>{
 const link=document.getElementById('feishuTableLink');
 if(!link)return;
 try{
  const response=await fetch('/api/viral-elements/links');
  if(!response.ok)throw Error('无法读取飞书链接');
  const data=await response.json();
  if(!data.elements_url){link.textContent='飞书表未配置';link.title='请先配置当前环境的飞书元素表';return;}
  const url=new URL(data.elements_url);
  if(url.protocol!=='https:'||!/(^|\.)(feishu\.cn|larksuite\.com)$/.test(url.hostname))throw Error('飞书链接无效');
  link.href=url.href;link.removeAttribute('aria-disabled');
 }catch(e){link.textContent='飞书链接暂不可用';link.title=e.message;}
})();
