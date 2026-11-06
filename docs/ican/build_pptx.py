# -*- coding: utf-8 -*-
"""Offline PPTX builder (stdlib only). Generates VideoInsight Agent iCAN pitch deck."""
import zipfile, os, datetime

EMU = 914400
SW = 12192000   # 13.333 in
SH = 6858000    # 7.5 in

NAVY="0F172A"; BLUE="3B82F6"; CYAN="06B6D4"; INK="1A1A1A"
SLATE="475569"; LIGHT="F1F5F9"; WHITE="FFFFFF"; AMBER="F59E0B"; PALE="EFF6FF"

def esc(s):
    return (str(s).replace('&','&amp;').replace('<','&lt;')
            .replace('>','&gt;').replace('"','&quot;'))

def sp_text(x,y,w,h, lines, fill=None, line=None, anchor='t', align='l'):
    """lines: list of (text, size_pt*100, bold_bool, color)"""
    paras=''
    for (t,sz,b,c) in lines:
        bv='1' if b else '0'
        paras += (f'<a:p><a:pPr algn="{align}"/>'
                  f'<a:r><a:rPr lang="zh-CN" sz="{sz}" b="{bv}">'
                  f'<a:solidFill><a:srgbClr val="{c}"/></a:solidFill>'
                  f'</a:rPr><a:t>{esc(t)}</a:t></a:r></a:p>')
    fillxml = f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>' if fill else '<a:noFill/>'
    linexml = f'<a:ln><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>' if line else '<a:ln><a:noFill/></a:ln>'
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sp_text.i}" name="bx{sp_text.i}"/>'
            f'<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>{fillxml}{linexml}</p:spPr>'
            f'<p:txBody><a:bodyPr wrap="square" lIns="120000" tIns="60000" rIns="120000" bIns="60000" anchor="{anchor}"/>'
            f'<a:lstStyle/>{paras}</p:txBody></p:sp>')
sp_text.i = 2

def sp_rect(x,y,w,h, fill=None, line=None):
    fillxml = f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>' if fill else '<a:noFill/>'
    linexml = f'<a:ln><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln>' if line else '<a:ln><a:noFill/></a:ln>'
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sp_text.i}" name="r{sp_text.i}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>{fillxml}{linexml}</p:spPr>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="zh-CN"/></a:p></p:txBody></p:sp>')
sp_text.i = 2

def slide(bg, shapes):
    bgxml = (f'<p:bg><p:bgPr><a:solidFill><a:srgbClr val="{bg}"/></a:solidFill>'
             f'<a:effectLst/></p:bgPr></p:bg>') if bg else ''
    tree = ''.join(shapes)
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            f'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            f'<p:cSld>{bgxml}<p:spTree>'
            f'<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            f'<p:grpSpPr><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></p:grpSpPr>'
            f'{tree}</p:spTree></p:cSld>'
            f'<p:clrMapOvr><a:overrideClrMapping bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" '
            f'accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" '
            f'accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/></p:clrMapOvr>'
            f'</p:sld>')

def title_bar(title, sub=None):
    """Standard light content-slide title bar (returns shapes list)."""
    s=[]
    s.append(sp_rect(0,0,SW,150*EMU//100 if False else 1100000, fill=BLUE))
    s.append(sp_text(600000,230000, 9000000, 700000,
        [(title, 2600, True, WHITE)], anchor='c'))
    if sub:
        s.append(sp_text(600000,780000, 9000000, 360000,
            [(sub, 1200, False, "DCE9FF")], anchor='c'))
    return s

# ---------- build slides ----------
slides=[]

# 1. Cover
sp_text.i=2
s=[]
s.append(sp_rect(0,0,SW,SH,fill=NAVY))
s.append(sp_rect(0,0,1400000,SH,fill=BLUE))
s.append(sp_text(1700000,1400000, 9800000, 900000,
    [("见影知著", 5200, True, WHITE)], anchor='c'))
s.append(sp_text(1700000,2350000, 9800000, 600000,
    [("AI 视频知识解析与追问系统 · VideoInsight Agent", 2600, True, CYAN)], anchor='c'))
s.append(sp_text(1700000,3050000, 9800000, 500000,
    [("粘贴一条视频链接，自动完成 下载 · 转写 · 总结 · 评论 · 可追问 AI 助手", 1600, False, "CBD5E1")], anchor='c'))
s.append(sp_text(1700000,5400000, 9800000, 400000,
    [("iCAN 大学生创新创业大赛 · 创新赛道（双盲评审）", 1500, False, "94A3B8")], anchor='c'))
s.append(sp_text(1700000,5800000, 9800000, 400000,
    [("参赛学生：姓名1 / 姓名2 / 姓名3   ｜   【本材料含 AI 辅助制作】", 1400, False, "64748B")], anchor='c'))
slides.append(slide(NAVY, s))

# 2. Catalog
sp_text.i=2
s=[]
s+=title_bar("目录  /  Contents")
items=[("01","项目与痛点","市场背景 · 行业痛点"),
       ("02","技术与功能","技术架构 · 核心功能"),
       ("03","价值与场景","产品优点 · 使用方法 · 应用场景"),
       ("04","反思与展望","改进建议 · 总结展望")]
y=1500000
for (num,t,d) in items:
    s.append(sp_rect(600000,y,1100000,720000,fill=PALE))
    s.append(sp_text(600000,y,1100000,720000,[(num,3000,True,BLUE)],anchor='c',align='c'))
    s.append(sp_text(1900000,y+40000,8000000,400000,[(t,2000,True,INK)],anchor='c'))
    s.append(sp_text(1900000,y+420000,8000000,300000,[(d,1300,False,SLATE)],anchor='c'))
    y+=820000
slides.append(slide(WHITE, s))

# 3. Section 01
sp_text.i=2
s=[]
s.append(sp_rect(0,0,SW,SH,fill=NAVY))
s.append(sp_rect(0,SH//2-30000,900000,60000,fill=BLUE))
s.append(sp_text(900000,2600000,10000000,900000,[("01",6400,True,BLUE)],anchor='c'))
s.append(sp_text(900000,3500000,10000000,800000,[("项目与痛点",3600,True,WHITE)],anchor='c'))
s.append(sp_text(900000,4400000,10000000,500000,[("Project & Pain Points",1700,False,CYAN)],anchor='c'))
slides.append(slide(NAVY, s))

# 4. 项目背景与痛点
sp_text.i=2
s=[]
s+=title_bar("项目概述 · 市场背景","为何是现在、为何是这件事")
# left big number
s.append(sp_rect(600000,1400000,5200000,1700000,fill=LIGHT))
s.append(sp_text(600000,1500000,5200000,900000,[("10.5亿",6000,True,BLUE)],anchor='c',align='c'))
s.append(sp_text(600000,2450000,5200000,500000,[("国内短视频用户规模（CNNIC 2024.6）",1400,False,SLATE)],anchor='c',align='c'))
s.append(sp_text(600000,3000000,5200000,60000,[]))  # spacer
s.append(sp_rect(600000,3300000,5200000,1300000,fill=PALE))
s.append(sp_text(700000,3400000,5000000,1200000,[
    ("政策驱动",1700,True,INK),
    ("2024《政府工作报告》提出",1300,False,SLATE),
    ("开展“人工智能+”行动；",1300,False,SLATE),
    ("《新一代人工智能发展规划》",1300,False,SLATE),
    ("要求 AI 与各行业深度融合。",1300,False,SLATE)],anchor='t'))
# right 4 pain points
px=6200000; pw=5400000; py=1400000; ph=720000; gap=90000
pains=[("获取难","抖音 a_bogus 签名墙 + 第三方站不稳，无水印源难拿"),
       ("整理慢","手动转写+喂模型+整理，长视频吸收以小时计"),
       ("不闭环","文字到手无法追问，评论反馈未被利用"),
       ("难复用","通用工具只跑一次，无持久化与故障恢复")]
for (t,d) in pains:
    s.append(sp_rect(px,py,pw,ph,fill=WHITE,line="D7DEE8"))
    s.append(sp_text(px+200000,py+80000,pw-400000,300000,[(t,1600,True,BLUE)]))
    s.append(sp_text(px+200000,py+360000,pw-400000,340000,[(d,1200,False,SLATE)]))
    py+=ph+gap
slides.append(slide(WHITE, s))

# 5. Section 02
sp_text.i=2
s=[]
s.append(sp_rect(0,0,SW,SH,fill=NAVY))
s.append(sp_rect(0,SH//2-30000,900000,60000,fill=BLUE))
s.append(sp_text(900000,2600000,10000000,900000,[("02",6400,True,BLUE)],anchor='c'))
s.append(sp_text(900000,3500000,10000000,800000,[("技术与功能",3600,True,WHITE)],anchor='c'))
s.append(sp_text(900000,4400000,10000000,500000,[("Architecture & Functions",1700,False,CYAN)],anchor='c'))
slides.append(slide(NAVY, s))

# 6. 技术架构
sp_text.i=2
s=[]
s+=title_bar("技术架构  ·  数据—模型—服务—应用")
layers=[("数据层 Data","链接解析归一化 · 多平台下载（抖音免签名直连 / yt-dlp 兜底）· ffmpeg 16kHz 音频预处理",BLUE),
        ("模型层 Model","DashScope Paraformer 转写（长音频切片并发）· OpenAI-compatible LLM 结构化分析与多轮问答",CYAN),
        ("服务层 Service","FastAPI 异步 · SQLite 按身份隔离 · 有界并发 · 重启对账 · 产物回收 · 可选共享令牌鉴权",BLUE),
        ("应用层 App","原生 Web 四视图（总结/逐字稿/评论/JSON）· 在线播放 · AI 追问 · 深浅主题",CYAN)]
y=1400000
for (t,d,c) in layers:
    s.append(sp_rect(600000,y,1100000,800000,fill=c))
    s.append(sp_text(600000,y,1100000,800000,[(t,1500,True,WHITE)],anchor='c',align='c'))
    s.append(sp_rect(1850000,y,10300000,800000,fill=LIGHT))
    s.append(sp_text(2050000,y,9900000,800000,[(d,1400,False,INK)],anchor='c'))
    y+=900000
s.append(sp_text(600000,y+80000,11400000,360000,
    [("“数据—模型—服务—应用”四层架构，把分散的下载/转写/分析/问答收敛成一个可复用系统。",1300,False,SLATE)]))
slides.append(slide(WHITE, s))

# 7. 核心功能
sp_text.i=2
s=[]
s+=title_bar("核心功能  ·  4 大模块","痛点 ↔ 功能 1:1 对应")
mods=[("① 多平台链接即获取","支持抖音/快手/B站/小红书 4 平台；抖音免签名直连 + 本地上传（≤2048MB）双兜底",BLUE),
      ("② 语音转写与结构化总结",">900s 切片并发转写；转写吞吐≈8×实时（自学习校准）；transcript/summary/interview 三模式",CYAN),
      ("③ 评论接入与可追问助手","抖音评论零依赖抓取；失败静默降级不阻断主链路；基于转写稿多轮追问",BLUE),
      ("④ 稳定可复用服务体系","178 项自动化测试通过；默认 2 路有界并发 + 重启自动对账；可选鉴权中间件",CYAN)]
y=1400000
for (t,d,c) in mods:
    s.append(sp_rect(600000,y,11400000,640000,fill=WHITE,line="D7DEE8"))
    s.append(sp_rect(600000,y,140000,640000,fill=c))
    s.append(sp_text(900000,y+60000,7000000,320000,[(t,1700,True,INK)]))
    s.append(sp_text(900000,y+360000,10500000,260000,[(d,1250,False,SLATE)]))
    y+=740000
slides.append(slide(WHITE, s))

# 8. Section 03
sp_text.i=2
s=[]
s.append(sp_rect(0,0,SW,SH,fill=NAVY))
s.append(sp_rect(0,SH//2-30000,900000,60000,fill=BLUE))
s.append(sp_text(900000,2600000,10000000,900000,[("03",6400,True,BLUE)],anchor='c'))
s.append(sp_text(900000,3500000,10000000,800000,[("价值与场景",3600,True,WHITE)],anchor='c'))
s.append(sp_text(900000,4400000,10000000,500000,[("Value & Scenarios",1700,False,CYAN)],anchor='c'))
slides.append(slide(NAVY, s))

# 9. 产品优点
sp_text.i=2
s=[]
s+=title_bar("产品优点","三个卖点：敢用 · 能用 · 好用")
adv=[("智能化获取","通过移动分享页直连与多源兜底，抖音免签名下载成功率显著提升，响应为秒级提交、分钟级出结果。",BLUE),
     ("全链路闭环","融合下载—转写—总结—评论—追问多环节，复杂平台环境下仍保持主链路可靠；可选鉴权符合基础安全合规。",CYAN),
     ("多场景适应","面向求职复盘、课程摘要、内容创作、企业内训等 4+ 类场景，兼容 4 平台与本地文件，降低集成门槛。",BLUE)]
y=1450000
for (t,d,c) in adv:
    s.append(sp_rect(600000,y,11400000,680000,fill=LIGHT))
    s.append(sp_rect(600000,y,140000,680000,fill=c))
    s.append(sp_text(950000,y+70000,3600000,400000,[(t,1900,True,c)]))
    s.append(sp_text(4800000,y+70000,6700000,560000,[(d,1350,False,INK)],anchor='c'))
    y+=780000
slides.append(slide(WHITE, s))

# 10. 使用方法
sp_text.i=2
s=[]
s+=title_bar("使用方法","三步上手，零配置可跑（内置 Mock 开箱即用）")
steps=[("① 接入","浏览器访问 Web 端，或调用 POST /analyze 接口集成。",BLUE),
       ("② 提交","粘贴链接（或上传文件）→ 选模式：transcript / summary / interview。",CYAN),
       ("③ 查看","实时看进度、阶段与 ETA；结果四视图一键切换，随时 AI 追问。",BLUE)]
y=1500000
for (t,d,c) in steps:
    s.append(sp_rect(600000,y,11400000,640000,fill=WHITE,line="D7DEE8"))
    s.append(sp_rect(600000,y,1100000,640000,fill=c))
    s.append(sp_text(600000,y,1100000,640000,[(t,2000,True,WHITE)],anchor='c',align='c'))
    s.append(sp_text(1900000,y+120000,9200000,400000,[(d,1500,False,INK)],anchor='c'))
    y+=740000
slides.append(slide(WHITE, s))

# 11. 应用场景
sp_text.i=2
s=[]
s+=title_bar("应用场景","学习 · 创作 · 企业 三类场景均已闭环可用")
sc=[("求职 / 学习复盘","面试视频、技术分享 → interview 模式提取面经与要点，单条吸收从小时级压缩至分钟级。",BLUE),
    ("内容创作 / 竞品","抖音/B站热门 → 逐字稿 + 评论热词 + 结构化摘要，辅助选题与脚本整理。",CYAN),
    ("企业 / 团队内训","培训视频本地私有部署，原始音视频可一键删除，知识从“看完即忘”变“可检索复用”。",BLUE)]
y=1450000
for (t,d,c) in sc:
    s.append(sp_rect(600000,y,11400000,680000,fill=LIGHT))
    s.append(sp_rect(600000,y,140000,680000,fill=c))
    s.append(sp_text(950000,y+70000,3800000,400000,[(t,1750,True,c)]))
    s.append(sp_text(4900000,y+70000,6600000,560000,[(d,1350,False,INK)],anchor='c'))
    y+=780000
slides.append(slide(WHITE, s))

# 12. Section 04
sp_text.i=2
s=[]
s.append(sp_rect(0,0,SW,SH,fill=NAVY))
s.append(sp_rect(0,SH//2-30000,900000,60000,fill=BLUE))
s.append(sp_text(900000,2600000,10000000,900000,[("04",6400,True,BLUE)],anchor='c'))
s.append(sp_text(900000,3500000,10000000,800000,[("反思与展望",3600,True,WHITE)],anchor='c'))
s.append(sp_text(900000,4400000,10000000,500000,[("Reflection & Outlook",1700,False,CYAN)],anchor='c'))
slides.append(slide(NAVY, s))

# 13. 改进建议
sp_text.i=2
s=[]
s+=title_bar("改进建议","反套路坦诚：主动暴露短板（标注为当前差距 / 目标值）")
imp=[("技术优化","ASR 中文准确率依赖云端模型，离线/弱网受限；多平台评论需外接 MediaCrawler（规划值）。",BLUE),
     ("运营挑战","当前为单机 SQLite；横向扩展需引入 Celery+Redis、PostgreSQL（规划值）。",CYAN),
     ("安全风险","鉴权为单共享密钥，尚无账号体系与权限粒度（规划值）。",BLUE),
     ("标准合规","各平台下载接口频繁变动（如抖音 a_bogus），需持续维护适配。",CYAN)]
x0=600000; y0=1450000; cw=5600000; ch=900000; gx=200000; gy=120000
pos=[(x0,y0),(x0+cw+gx,y0),(x0,y0+ch+gy),(x0+cw+gx,y0+ch+gy)]
for i,(t,d,c) in enumerate(imp):
    (x,y)=pos[i]
    s.append(sp_rect(x,y,cw,ch,fill=LIGHT))
    s.append(sp_rect(x,y,cw,120000,fill=c))
    s.append(sp_text(x+200000,y+170000,cw-400000,300000,[(t,1600,True,INK)]))
    s.append(sp_text(x+200000,y+450000,cw-400000,420000,[(d,1200,False,SLATE)]))
slides.append(slide(WHITE, s))

# 14. 总结展望
sp_text.i=2
s=[]
s+=title_bar("总结与展望")
s.append(sp_rect(600000,1400000,11400000,1500000,fill=PALE))
s.append(sp_text(800000,1500000,11000000,1400000,[
    ("总结",1900,True,BLUE),
    ("首创“数据—模型—服务—应用”四层视频解析架构，解决传统人工链路慢、散、不可复用的问题；",1350,False,INK),
    ("提供 Web 端与 API 开放调用；本地私有部署优先、原始音视频可一键删除，内置可选鉴权。",1350,False,INK)],anchor='t'))
s.append(sp_rect(600000,3100000,11400000,1700000,fill=NAVY))
s.append(sp_text(900000,3250000,10800000,1500000,[
    ("愿景",1900,True,CYAN),
    ("成为视频知识吸收的基础设施——",1600,True,WHITE),
    ("“一个链接，几分钟，把别人的视频变成自己的知识。”",2200,True,WHITE)],anchor='c'))
slides.append(slide(WHITE, s))

# 15. 封底
sp_text.i=2
s=[]
s.append(sp_rect(0,0,SW,SH,fill=NAVY))
s.append(sp_rect(0,0,1400000,SH,fill=BLUE))
s.append(sp_text(1700000,2200000,9800000,900000,[("见影知著",4800,True,WHITE)],anchor='c'))
s.append(sp_text(1700000,3150000,9800000,600000,[("敬请各位专家评委批评指正",2200,True,CYAN)],anchor='c'))
s.append(sp_text(1700000,4500000,9800000,400000,[("参赛学生：姓名1 / 姓名2 / 姓名3",1500,False,"94A3B8")],anchor='c'))
s.append(sp_text(1700000,4950000,9800000,400000,[("本材料含 AI 辅助制作 ｜ 待补项与答辩追问预案见参赛策划书",1300,False,"64748B")],anchor='c'))
slides.append(slide(NAVY, s))

# ---------- package ----------
n=len(slides)
ct='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'''
for i in range(2,n+1):
    ct+=f'\n<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
ct+='\n<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
ct+='\n<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
ct+='\n</Types>'

root_rels='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''

sldid=''.join(f'<p:sldId id="{256+i}" r:id="rId{i+1}"/>' for i in range(n))
pres=f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId{n+1}"/></p:sldMasterIdLst>
<p:sldIdLst>{sldid}</p:sldIdLst>
<p:sldSz cx="{SW}" cy="{SH}" type="screen16x9"/>
<p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>'''

pres_rels=f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId{n+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'''
for i in range(n):
    pres_rels+=f'\n<Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i+1}.xml"/>'
pres_rels+='\n</Relationships>'

master=f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>
<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
<p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles>
</p:sldMaster>'''

master_rels='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>'''

layout='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">
<p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree></p:cSld>
<p:clrMapOvr><a:overrideClrMapping bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/></p:clrMapOvr>
</p:sldLayout>'''

layout_rels='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>'''

theme='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">
<a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1F3864"/></a:dk2><a:lt2><a:srgbClr val="EEF1F6"/></a:lt2><a:accent1><a:srgbClr val="3B82F6"/></a:accent1><a:accent2><a:srgbClr val="06B6D4"/></a:accent2><a:accent3><a:srgbClr val="F59E0B"/></a:accent3><a:accent4><a:srgbClr val="475569"/></a:accent4><a:accent5><a:srgbClr val="0F172A"/></a:accent5><a:accent6><a:srgbClr val="FFFFFF"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme>
<a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>
<a:fmtScheme name="Office"><a:fillStyleLst/><a:lnStyleLst/><a:effectStyleLst/><a:bgFillStyleLst/></a:fmtScheme></a:themeElements></a:theme>'''

core=f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>见影知著 · AI 视频知识解析与追问系统 参赛策划书</dc:title>
<dc:creator>见影知著 团队</dc:creator>
<cp:lastModifiedBy>见影知著 团队</cp:lastModifiedBy>
<dcterms:created xsi:type="dcterms:W3CDTF">{datetime.date.today().isoformat()}T00:00:00Z</dcterms:created>
<dcterms:modified xsi:type="dcterms:W3CDTF">{datetime.date.today().isoformat()}T00:00:00Z</dcterms:modified>
</cp:coreProperties>'''

app='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
<Application>WorkBuddy</Application><Slides>%d</Slides></Properties>''' % n

out=r"C:\Users\Ynchen\Desktop\VideoInsight Agent\docs\ican\VideoInsightAgent_参赛策划书.pptx"
with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
    z.writestr('[Content_Types].xml', ct)
    z.writestr('_rels/.rels', root_rels)
    z.writestr('ppt/presentation.xml', pres)
    z.writestr('ppt/_rels/presentation.xml.rels', pres_rels)
    z.writestr('ppt/slideMasters/slideMaster1.xml', master)
    z.writestr('ppt/slideMasters/_rels/slideMaster1.xml.rels', master_rels)
    z.writestr('ppt/slideLayouts/slideLayout1.xml', layout)
    z.writestr('ppt/slideLayouts/_rels/slideLayout1.xml.rels', layout_rels)
    z.writestr('ppt/theme/theme1.xml', theme)
    for i,sl in enumerate(slides):
        z.writestr(f'ppt/slides/slide{i+1}.xml', sl)
    z.writestr('docProps/core.xml', core)
    z.writestr('docProps/app.xml', app)
print("WROTE", out, "slides=", n)
