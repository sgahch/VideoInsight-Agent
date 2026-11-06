async (page) => {
  const id="a".repeat(32), base="http://127.0.0.1:8011";
  const errors=[];
  page.on("pageerror",error=>errors.push(error.message));
  const rows=Array.from({length:125},(_,i)=>({
    comment_id:String(i),parent_comment_id:null,root_comment_id:null,
    content:`主评论 ${i} <script>不会执行</script>`,author_nickname:`观众 ${i}`,like_count:i,
    create_time:1700000000+i,reply_count:i===124?25:0,pictures:[],
  }));
  rows.push(...Array.from({length:25},(_,i)=>({
    comment_id:`reply-${i}`,parent_comment_id:i?"reply-0":"124",root_comment_id:"124",
    content:`子评论 ${i}`,author_nickname:`回复者 ${i}`,like_count:1000+i,
    create_time:1700000200000+i*1000,reply_count:0,pictures:[],
  })));
  const job={id,platform:"bili",status:"completed",url:"https://www.bilibili.com/video/BV1test",
    total:150,roots:125,replies:25,pages:10,created:1700000000,message:"本地交互测试数据"};
  const requests=[];
  await page.route("**/comment-api/**",async route=>{
    const url=new URL(route.request().url()), params=url.searchParams;
    requests.push(url.pathname+url.search);
    let result;
    if(url.pathname.endsWith("/platforms"))result=[{platform:"bili",name:"B站",cookies_configured:false}];
    else if(url.pathname.endsWith("/jobs"))result=[job];
    else if(url.pathname.endsWith("/comments")) {
      let items=rows.filter(row=>{
        const scope=params.get("scope");
        return (scope!=="roots"||!row.root_comment_id)&&(scope!=="replies"||!!row.root_comment_id)&&
          (!params.has("root_id")||row.root_comment_id===params.get("root_id"));
      });
      const matched=items.length, sort=params.get("sort");
      if(sort==="likes")items.sort((a,b)=>b.like_count-a.like_count);
      if(sort==="newest")items.sort((a,b)=>Number(b.create_time)-Number(a.create_time));
      if(params.has("top"))items=items.slice(0,Number(params.get("top")));
      const total=items.length, offset=Number(params.get("offset")||0), limit=Number(params.get("limit")||100);
      result={items:items.slice(offset,offset+limit),total,matched_total:matched,job_total:150};
    } else result=job;
    await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(result)});
  });
  await page.setViewportSize({width:1440,height:1100});
  await page.goto(`${base}/comments?job=${id}`);
  await page.locator("#commentList > article").first().waitFor();
  const assert=(value,message)=>{if(!value)throw Error(message);};
  assert(await page.locator("#commentList > article").count()===100,"Default root page size");
  assert(await page.locator("#commentList > article").first().getAttribute("data-comment-id")==="124","Global likes order");
  await page.getByRole("button",{name:"展开回复 (25)",exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll(".reply-thread article").length===20);
  assert(await page.locator(".reply-thread article").count()===20,"Lazy reply page");
  await page.getByRole("button",{name:"更多回复",exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll(".reply-thread article").length===25);
  assert(await page.locator(".reply-thread article").count()===25,"Reply pagination");
  await page.locator("#commentSort").selectOption("top10");
  await page.waitForFunction(()=>document.querySelectorAll("#commentList > article").length===10);
  assert(await page.locator("#next").isDisabled(),"Top ten has no second page");
  assert(await page.locator("#pageSize").isDisabled(),"Top ten ignores page size");
  assert((await page.locator("#exportFiltered").getAttribute("href")).includes("top=10"),"Filtered export");
  assert(!(await page.locator("#export").getAttribute("href")).includes("?"),"Unfiltered full export");
  await page.locator("#commentScope").selectOption("replies");
  await page.waitForFunction(()=>document.querySelector("#commentList > article")?.dataset.commentId==="reply-24");
  await page.locator("#commentSort").selectOption("newest");
  await page.waitForFunction(()=>document.querySelectorAll("#commentList > article").length===25);
  assert(await page.locator("#commentList > article").first().getAttribute("data-comment-id")==="reply-24","Newest order");
  await page.locator("#pageSize").selectOption("20");
  await page.waitForFunction(()=>document.querySelectorAll("#commentList > article").length===20);
  await page.locator("#next").click();
  await page.waitForFunction(()=>document.querySelectorAll("#commentList > article").length===5);
  await page.locator("#commentScope").selectOption("roots");
  await page.waitForFunction(()=>document.querySelector("#pageInfo")?.textContent.startsWith("1"));
  assert(await page.locator("#previous").isDisabled(),"Filter resets offset");
  await page.locator("#commentSort").selectOption("top10");
  await page.waitForFunction(()=>document.querySelectorAll("#commentList > article").length===10);
  await page.locator("#detailTitle").scrollIntoViewIfNeeded();
  await page.screenshot({path:"output/playwright/comments-desktop.png"});
  await page.setViewportSize({width:390,height:844});
  await page.locator("#detailTitle").scrollIntoViewIfNeeded();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),"Mobile horizontal overflow");
  const overlap=await page.locator(".comment-filters select").evaluateAll(nodes=>{
    const boxes=nodes.map(node=>node.getBoundingClientRect());
    return boxes.some((a,i)=>boxes.some((b,j)=>j>i&&a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top));
  });
  assert(!overlap,"Mobile controls overlap");
  await page.screenshot({path:"output/playwright/comments-mobile.png"});
  const inlineComments=rows.slice(0,25).map(row=>({...row,sub_comments:[]}));
  inlineComments[0].sub_comments=[{...rows[125],sub_comments:[]}];
  const task={task_id:"inline-smoke",status:"succeeded",created_at:"2026-11-05T00:00:00Z",
    updated_at:"2026-11-05T00:00:00Z",request:{url:"https://www.douyin.com/video/123",mode:"summary"},
    title:"评论交互测试",result:{output:"测试总结",transcript:"测试转写",comments:inlineComments,
      diagnostics:{comments_source:"抖音评论接口::123"}}};
  await page.route("**/tasks**",async route=>{
    const url=new URL(route.request().url());
    await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(url.pathname==="/tasks"?[task]:task)});
  });
  await page.setViewportSize({width:1440,height:1100});
  await page.goto(base);
  await page.locator('[data-task-id="inline-smoke"]').click();
  await page.getByRole("tab",{name:"观众评论",exact:true}).click();
  await page.locator("#commentsOutput .comments-filter-toolbar").waitFor();
  assert((await page.locator("#commentsOutput").textContent()).includes("旧版抖音热门样本"),"Sample scope warning");
  await page.locator("#commentsOutput").getByLabel("排序",{exact:true}).selectOption("top10");
  assert(await page.locator("#commentsOutput .comment-list > li").count()===10,"Inline top ten");
  await page.locator("#commentsOutput").getByLabel("范围",{exact:true}).selectOption("replies");
  assert(await page.locator("#commentsOutput .comment-list > li").count()===1,"Inline reply-only scope");
  await page.locator("#commentsOutput").getByLabel("范围",{exact:true}).selectOption("roots");
  await page.locator("#commentsOutput").getByLabel("排序",{exact:true}).selectOption("newest");
  assert((await page.locator("#commentsOutput .comment-list > li").first().textContent()).includes("主评论 24"),"Inline newest");
  await page.screenshot({path:"output/playwright/comments-inline-desktop.png"});
  await page.setViewportSize({width:390,height:844});
  await page.locator("#commentsOutput .comments-filter-toolbar").scrollIntoViewIfNeeded();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),"Inline mobile overflow");
  await page.screenshot({path:"output/playwright/comments-inline-mobile.png"});
  assert(errors.length===0,`Page errors: ${errors.join(", ")}`);
  await page.unroute("**/comment-api/**");
  await page.unroute("**/tasks**");
  return {rows:rows.length,globalTop10:true,replies:25,inline:true,mobileWidth:390,errors,requests:requests.length};
}
