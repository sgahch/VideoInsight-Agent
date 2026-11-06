(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const names = {dy:"抖音",ks:"快手",bili:"B站",xhs:"小红书"};
  const labels = {queued:"排队中",running:"抓取中",completed:"已遍历完成",needs_login:"等待登录",blocked:"需要处理",partial:"部分完成",failed:"抓取失败",cancelled:"已停止",interrupted:"待继续"};
  const active = (job) => job && ["queued","running"].includes(job.status);
  let selected = new URLSearchParams(location.search).get("job"), offset = 0, current = null, busy = false, poll = null;
  let detailVersion = 0, renderedPage = "";
  const {formatTime} = CommentUtils;
  function filters() {
    const mode = $("commentSort").value;
    const params = new URLSearchParams({sort:mode==="top10"?"likes":mode,scope:$("commentScope").value});
    if(mode==="top10")params.set("top","10");
    return params;
  }
  function commentNode(item, expand = true) {
    const jobId=selected, replySort=filters().get("sort");
    const node=el("article",undefined,`comment ${item.root_comment_id?"reply":""}`);
    node.dataset.commentId=item.comment_id;
    const head=el("div",undefined,"comment-head");
    head.append(el("strong",item.author_nickname||"观众"),el("span",`赞 ${item.like_count.toLocaleString()}`));
    node.append(head,el("p",item.content||"（无文字内容）"));
    const meta=el("div",undefined,"comment-meta");
    meta.append(el("time",formatTime(item.create_time)));
    if(item.root_comment_id)meta.append(el("span",`回复 ${item.parent_comment_id||item.root_comment_id}`));
    node.append(meta);
    const pictures=(item.pictures||[]).filter(url=>/^https?:\/\//i.test(url));
    if(pictures.length) {
      const media=el("div",undefined,"comment-pictures");
      for(const url of pictures){const link=el("a");link.href=url;link.target="_blank";link.rel="noopener noreferrer";const image=el("img");image.src=url;image.alt="评论图片";image.loading="lazy";image.referrerPolicy="no-referrer";link.append(image);media.append(link);}
      node.append(media);
    }
    if(expand&&!item.root_comment_id&&item.reply_count>0) {
      const toggle=el("button",`展开回复 (${item.reply_count})`,"text-button reply-toggle");
      toggle.type="button";toggle.setAttribute("aria-expanded","false");
      const thread=el("div",undefined,"reply-thread");thread.hidden=true;
      let loaded=false, replyOffset=0;
      const list=el("div"), more=el("button","更多回复","secondary");
      more.type="button";
      async function load() {
        more.disabled=true;toggle.disabled=true;
        try {
          const params=new URLSearchParams({scope:"replies",root_id:item.comment_id,sort:replySort,offset:String(replyOffset),limit:"20"});
          const page=await api(`/jobs/${jobId}/comments?${params}`);
          if(!node.isConnected||selected!==jobId)return;
          list.append(...page.items.map(row=>commentNode(row,false)));
          replyOffset+=page.items.length;loaded=true;more.hidden=replyOffset>=page.total;
          if(!page.total)list.replaceChildren(el("p","暂无已入库回复。","muted"));
        } catch(error){notice(error.message,true);} finally {more.disabled=false;toggle.disabled=false;}
      }
      more.onclick=load;
      toggle.onclick=async()=>{
        thread.hidden=!thread.hidden;toggle.setAttribute("aria-expanded",String(!thread.hidden));
        toggle.textContent=thread.hidden?`展开回复 (${item.reply_count})`:"收起回复";
        if(!thread.hidden&&!loaded)await load();
      };
      thread.append(list,more);node.append(toggle,thread);
    }
    return node;
  }
  let clientId = localStorage.getItem("videoinsight_client_id");
  if (!clientId) {clientId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2,12)}`;localStorage.setItem("videoinsight_client_id",clientId);}
  document.cookie = `videoinsight_client_id=${encodeURIComponent(clientId)}; Path=/; SameSite=Lax; Max-Age=31536000`;
  async function api(path, options = {}) {
    const headers = {"X-Client-Id":clientId,...options.headers};
    const token = localStorage.getItem("videoinsight_auth_token");
    if (token) headers["X-Auth-Token"] = token;
    if (options.body) headers["Content-Type"] = "application/json";
    const response = await fetch(`/comment-api${path}`,{...options,headers});
    if (response.status === 401) {location.assign("/login");throw Error("请重新登录。");}
    const data = await response.json();
    if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : "请求失败，请稍后重试。");
    return data;
  }
  function notice(message, error = false) {$("notice").textContent = message;$("notice").classList.toggle("error",error);$("notice").hidden = false;}
  function el(tag, text, cls) {const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;}
  async function platforms() {
    const values = await api("/platforms");
    $("platforms").replaceChildren(...values.map(p=>{const item=el("div",undefined,`platform ${p.cookies_configured?"configured":""}`);item.append(el("strong",p.name),el("span",p.cookies_configured?"已保存 · 待任务验证":"需要登录"));return item;}));
  }
  async function jobs() {
    const list=await api("/jobs");
    $("jobs").replaceChildren(...list.map(job=>{const button=el("button",undefined,`job ${job.id===selected?"selected":""}`);button.type="button";button.append(el("strong",`${names[job.platform]} · ${labels[job.status]||job.status}`),el("small",`${job.total.toLocaleString()} 条评论 · ${new Date(job.created*1000).toLocaleString()}`),el("small",job.url));button.onclick=()=>choose(job.id);return button;}));
    if(!list.length)$("jobs").append(el("p","还没有抓取记录。","muted"));
    if(!selected&&list.length){selected=list[0].id;offset=0;}
    if(selected)await detail();
  }
  async function choose(id) {selected=id;offset=0;renderedPage="";history.replaceState(null,"",`/comments?job=${encodeURIComponent(id)}`);await detail();await refresh();}
  async function detail() {
    const id = selected, version=++detailVersion, params=filters(), pageOffset=offset, size=Number($("pageSize").value);
    const job=await api(`/jobs/${id}`);
    if(id!==selected||version!==detailVersion)return;
    current=job;$("empty").hidden=true;$("detail").hidden=false;
    $("detailTitle").textContent=`${names[job.platform]}评论`;
    $("jobStatus").textContent=labels[job.status]||job.status;
    $("sourceUrl").href=job.url;$("sourceUrl").textContent=job.url;
    $("jobMessage").textContent=job.message||"等待任务开始。";
    $("stats").replaceChildren(...[[job.total,"全部评论"],[job.roots,"一级评论"],[job.replies,"子评论"],[job.pages,"已保存分页"]].map(([n,label])=>{const node=el("div",undefined,"stat");node.append(el("strong",n.toLocaleString()),el("span",label));return node;}));
    $("retry").hidden=active(job)||job.status==="completed"||job.source==="local-browser";
    $("cancel").hidden=!active(job);
    $("export").href=`/comment-api/jobs/${id}/export`;
    $("exportFiltered").href=`/comment-api/jobs/${id}/export?${params}`;
    params.set("offset",String(pageOffset));params.set("limit",String(size));
    const page=await api(`/jobs/${id}/comments?${params}`);
    if(id!==selected||version!==detailVersion)return;
    if(!Number.isFinite(page.matched_total))throw Error("评论服务版本较旧，请同步更新评论抓取服务以启用排序和筛选。");
    const signature=JSON.stringify([id,params.toString(),page.items]);
    if(signature!==renderedPage) {
      $("commentList").replaceChildren(...page.items.map(item=>commentNode(item)));
      renderedPage=signature;
    }
    if(!page.items.length)$("commentList").replaceChildren(el("p",job.total?"当前筛选下暂无评论。":job.status==="completed"?"该账号当前可见评论为 0 条。":"暂未入库评论，请查看上方任务状态。","muted"));
    $("rankingScope").textContent=`已采集 ${job.total.toLocaleString()} 条 · 当前范围 ${page.matched_total.toLocaleString()} 条${params.has("top")?" · 点赞数前 10":""}${job.status==="completed"?"":" · 采集中或尚未抓全，排名会变化"}`;
    $("pageInfo").textContent=`${page.items.length?pageOffset+1:0}–${pageOffset+page.items.length} / ${page.total.toLocaleString()}`;
    $("previous").disabled=pageOffset===0;$("next").disabled=pageOffset+size>=page.total;
  }
  async function refresh() {if(busy)return;busy=true;try{await jobs();}catch(error){notice(error.message,true);}finally{busy=false;}}
  $("cookieForm").onsubmit=async(event)=>{event.preventDefault();const button=event.submitter||event.currentTarget.querySelector('button[type="submit"]');button.disabled=true;try{await api(`/platforms/${$("cookiePlatform").value}/cookies`,{method:"PUT",body:JSON.stringify({content:$("cookieContent").value})});$("cookieContent").value="";$("cookieFile").value="";notice("Cookies 已保存。登录是否有效会在抓取时验证；已有等待任务请点击继续抓取。");await platforms();}catch(error){notice(error.message,true);}finally{button.disabled=false;}};
  $("cookieFile").onchange=async()=>{const file=$("cookieFile").files[0];if(!file)return;if(file.size>200000){notice("Cookies 文件过大，请导出对应平台的 Cookies。",true);return;}$("cookieContent").value=await file.text();};
  $("removeCookies").onclick=async()=>{try{await api(`/platforms/${$("cookiePlatform").value}/cookies`,{method:"DELETE"});$("cookieContent").value="";notice("已清除此平台的登录信息。");await platforms();}catch(error){notice(error.message,true);}};
  $("jobForm").onsubmit=async(event)=>{event.preventDefault();const button=event.submitter||event.currentTarget.querySelector('button[type="submit"]');button.disabled=true;try{const job=await api("/jobs",{method:"POST",body:JSON.stringify({url:$("videoUrl").value.trim()})});notice("已创建后台任务，一级评论和子评论会逐页保存。");await choose(job.id);}catch(error){notice(error.message,true);}finally{button.disabled=false;}};
  $("browserImportForm").onsubmit=async(event)=>{
    event.preventDefault();const button=event.submitter||event.currentTarget.querySelector('button[type="submit"]');button.disabled=true;
    try{
      const file=$("browserSnapshot").files[0];if(!file)throw Error("请选择 snapshot.json。");
      if(file.size>20*1024*1024)throw Error("采集文件超过 20 MB，请分任务采集。");
      let snapshot;try{snapshot=JSON.parse(await file.text());}catch{throw Error("文件不是有效 JSON。");}
      const job=await api("/import-browser",{method:"POST",body:JSON.stringify({snapshot})});
      notice(job.message||"本机采集结果已导入。");$("browserSnapshot").value="";await choose(job.id);
    }catch(error){notice(error.message,true);}finally{button.disabled=false;}
  };
  for(const action of ["retry","cancel"])$(action).onclick=async()=>{if(!current)return;$(action).disabled=true;try{await api(`/jobs/${current.id}/${action}`,{method:"POST"});await refresh();}catch(error){notice(error.message,true);}finally{$(action).disabled=false;}};
  $("previous").onclick=async()=>{offset=Math.max(0,offset-Number($("pageSize").value));try{await detail();}catch(error){notice(error.message,true);}};
  $("next").onclick=async()=>{offset+=Number($("pageSize").value);try{await detail();}catch(error){notice(error.message,true);}};
  for(const id of ["commentSort","commentScope","pageSize"])$(id).onchange=async()=>{
    offset=0;renderedPage="";$("pageSize").disabled=$("commentSort").value==="top10";
    if(selected)try{await detail();}catch(error){notice(error.message,true);}
  };
  $("refresh").onclick=async()=>{await refresh();try{await platforms();}catch(error){notice(error.message,true);}};
  const initialUrl=new URLSearchParams(location.search).get("url");
  if(initialUrl)$("videoUrl").value=initialUrl;
  Promise.all([platforms(),refresh()]).catch(error=>notice(error.message,true));
  poll=setInterval(()=>{if(!document.hidden)refresh();},4000);
  window.addEventListener("pagehide",()=>clearInterval(poll));
})();
