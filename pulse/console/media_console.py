"""素材库图形化控制台（所有者：root）。

纯标准库实现（http.server + 内嵌 HTML），不引入任何新依赖：

- ``GET  /``             素材库页面（上传、容量预警、冷却状态）
- ``GET  /api/state``    素材清单 + 容量报告
- ``POST /api/assets``   上传实拍素材入库（图片/视频，multipart/form-data，需视觉识别凭据）
- ``POST /api/describe`` 选图后自动生成描述（摘要 / 细节 / 关键词），
  识别成功时同时签发"入库许可"凭据
- ``POST /api/usage``    标记素材已进入内容 / 发布（触发 15 天冷却）
- ``POST /api/recall``   按策略召回候选（新图优先 + 冷却过滤 + 加权随机）
- ``GET  /api/asset-image`` 素材预览（按 asset_id 回传图片/视频，支持 Range）
- ``POST /api/caption``  按平台模式生成短文（报告 §3 的文案结构）

**入库许可（2026-09-11 新增）**：只有完成视觉识别的图片才允许入库。控制台上按钮在
识别成功前是灰色不可点的；服务端同样校验凭据，绕过页面直接调接口也进不来。
"""

from __future__ import annotations

import hashlib
import glob
import json
import os
import re
import secrets
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pulse import __version__
from pulse.services.media.catalog import MediaAsset, load_catalog
from pulse.services.media.categories import (
    CategoryCatalog,
    load_categories,
    resolve_category,
)
from pulse.services.media.config import (
    BRAND_NAME,
    REQUIRE_VISION_BEFORE_INGEST,
    VISION_TICKET_TTL_SECONDS,
    RecallConfig,
)
from pulse.services.media.describe import build_describer, heuristic_describe, read_env_file
from pulse.services.media.describe import VisionDescriber
from pulse.services.media.captions import (
    CAPTION_SPECS,
    caption_spec,
    generate_caption,
)
from pulse.services.media.describe import vision_config_from_env
from pulse.services.media.exporter import ExportEntry, export_entries
from pulse.services.media.video import extract_frames_from_bytes, is_video_name
from pulse.services.media.ingest import (
    DuplicateMediaError,
    MediaIngestor,
    UnsupportedMediaError,
)
from pulse.services.media.ledger import RecallLedger
from pulse.services.media.psd import (
    DEFAULT_MAX_SIDE as PSD_PREVIEW_MAX_SIDE,
    PSD_SUFFIXES,
    UnsupportedPsdError,
    to_png as psd_to_png,
)
from pulse.services.media.policy import MediaCandidate, RecallPolicy
from pulse.services.media.platforms import (
    RELAXED_LEVELS,
    level_label,
    platform_choices,
    platform_profile,
    prefer_media_kind,
    slot_pools,
    slot_score,
    top_tier,
    weakest_level,
)
from pulse.services.media.registry import DEFAULT_REGISTRY_NAME, MediaRegistry

PAGE_HTML = (
    "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'/>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
    "<title>Pulse 素材库控制台</title><style>"
    "body{font-family:'Microsoft YaHei',system-ui,sans-serif;margin:0;background:#f5f6f8}"
    "header{background:#1f4e79;color:#fff;padding:18px 28px}"
    "header h1{margin:0;font-size:20px}header p{margin:6px 0 0;font-size:13px;opacity:.85}"
    "main{padding:20px 28px 60px;max-width:1180px;margin:0 auto}"
    ".banner{padding:12px 16px;border-radius:6px;font-weight:600;margin-bottom:18px}"
    ".banner.red{background:#fdecea;color:#b3261e;border:1px solid #f3b8b2}"
    ".banner.ok{background:#e8f5e9;color:#1b5e20;border:1px solid #b7dfb9}"
    ".cards{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:20px}"
    ".card{background:#fff;border:1px solid #dcdfe4;border-radius:8px;padding:14px 18px;min-width:120px}"
    ".card b{display:block;font-size:24px;margin-top:4px}"
    "section{background:#fff;border:1px solid #dcdfe4;border-radius:8px;padding:18px;margin-bottom:20px}"
    "h2{font-size:16px;margin:0 0 12px}"
    # 上传表单用四列固定网格：原来用 auto-fit，列数不确定，列宽忽宽忽窄、
    # 标签基线也对不齐。改成确定性布局后各列标签同高、输入框同高。
    ".row{display:grid;gap:14px;align-items:end;"
    "grid-template-columns:minmax(0,1.6fr) minmax(0,1fr) minmax(0,1fr) auto}"
    ".field{display:flex;flex-direction:column;gap:6px;min-width:0}"
    "label{font-size:13px;color:#444;display:block;line-height:20px}"
    ".row .field label{height:20px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
    "input,select{width:100%;height:36px;padding:0 9px;border:1px solid #c9ced6;"
    "border-radius:5px;font:inherit;box-sizing:border-box;background:#fff}"
    "input[type=file]{padding:6px 9px}"
    "textarea{width:100%;padding:8px 9px;border:1px solid #c9ced6;border-radius:5px;"
    "font:inherit;box-sizing:border-box;resize:vertical}"
    ".stack{display:grid;gap:12px;margin-top:14px}"
    ".slot{background:#fff;border:1px solid #dcdfe4;border-radius:8px;padding:14px;"
    "margin-bottom:12px}"
    ".slot h3{font-size:14px;margin:0 0 8px}"
    ".slot .asset{background:#fcfcfd}"
    ".picks{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}"
    ".thumb{width:100%;height:150px;object-fit:cover;border-radius:4px;display:block;"
    "background:#eef1f4;margin-bottom:8px}"
    ".caption-box{border-top:1px dashed #dcdfe4;margin-top:16px;padding-top:14px;"
    "display:grid;gap:10px}"
    ".checks{list-style:none;padding:0;margin:0;font-size:12px;display:flex;"
    "flex-wrap:wrap;gap:6px 18px}"
    ".checks li.ok{color:#1b5e20}"
    ".checks li.bad{color:#b3261e;font-weight:600}"
    "@media (max-width:820px){.row{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}"
    "button{background:#1f4e79;color:#fff;border:0;border-radius:5px;padding:9px 16px;cursor:pointer}"
    ".row button{height:36px;white-space:nowrap}"
    ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}"
    ".asset{border:1px solid #e1e4e8;border-radius:6px;padding:12px;background:#fcfcfd}"
    ".asset h3{margin:0 0 6px;font-size:14px}"
    ".tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:10px;"
    "background:#eef2f7;color:#33475b}"
    ".tag.new{background:#e3f2fd;color:#0d47a1}"
    ".tag.cooling{background:#fff4e5;color:#9a5b00}"
    ".muted{color:#6b7280;font-size:12px}"
    # 禁用态必须仍然可读：早先用了近白字配浅灰底（对比度 1.42:1），
    # 按钮看着就是个灰块、标签根本读不出来。这里改成 4.8:1 的深灰字。
    "button[disabled]{background:#e4e7eb;color:#5b6472;border:1px solid #d3d7dd;"
    "cursor:not-allowed}"
    ".export-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;"
    "margin-top:16px;padding-top:14px;border-top:1px dashed #dcdfe4}"
    ".ghost{background:#fff;color:#1f4e79;border:1px solid #1f4e79}"
    ".hint{font-size:12px;color:#9a5b00;margin-top:6px}"
    ".hint.bad{color:#b3261e}"
    ".hint.ok{color:#1b5e20}"
    "</style></head><body>"
    "<header><h1>Pulse 素材库控制台</h1>"
    "<p>品牌级素材池：沧州菲美得 ｜ 冷却 15 天 ｜ 新图优先召回 ｜ 容量低于 112 张红色预警</p>"
    "</header><main>"
    "<div id='banner' class='banner ok'>加载中…</div>"
    "<div class='cards' id='cards'></div>"
    "<section><h2>三步上手</h2><p>"
    "第一步：在「上传实拍素材入库」里选好品类，把电脑里的产品实拍图或视频选进来；页面会自动识别画面内容。<br/>"
    "识别成功前，「上传并入库」按钮是<span class='muted'>灰色、点不动</span>的；"
    "识别成功后会变成蓝色，这时才能点。<br/>"
    "第二步：看最上方的横幅——绿色表示素材充足；红色表示可用图片少于 112 张（约一周用量），需要尽快补拍。<br/>"
    "第三步：某张图被内容用掉后，点它卡片上的“标记已用于内容”，这张图 15 天内不会再被选中。"
    "</p></section>"
    "<section><h2>上传实拍素材入库（图片 / 视频 / PSD）</h2>"
    "<p class='muted'>入库许可：素材必须先通过视觉识别。识别没成功，按钮保持灰色，无法入库。<br/>"
    "视频接口本身不支持视频输入，系统会自动抽 4 张静帧送识别（成本约等于一张原图）。<br/>"
    "Photoshop 文档（.psd）会先解出合并图再送识别与预览，导出时另存一份同画面的 PNG。</p>"
    "<div><button type='button' id='vision-check-btn' class='ghost'>视觉模型自检</button>"
    "<span class='hint' id='vision-check-result'></span></div>"
    "<form id='upload'>"
    "<div class='row'>"
    "<div class='field'><label>图片文件</label>"
    "<input type='file' name='file' accept='image/*,video/*,.psd' required/></div>"
    "<div class='field'><label>品类 process</label>"
    "<select name='process' id='process-select' required></select></div>"
    "<div class='field'><label>子类 sub_process</label>"
    # 子类**不能**加 required：像「人员」这种没有子目录的品类，唯一选项的值就是
    # 空字符串，浏览器会把空值判成"未选择"进而拦下整张表单（2026-09-15 的入库 BUG）。
    "<select name='sub_process' id='sub-process-select'></select></div>"
    "<div class='field'><label>入库许可</label>"
    "<button type='submit' id='upload-btn' disabled>上传并入库</button>"
    "</div></div>"
    "<div class='hint' id='upload-gate'>请先选择图片，识别成功后按钮才会变亮。</div>"
    "<div class='hint' id='category-note'></div>"
    "<input type='hidden' name='vision_ticket' id='vision-ticket' value=''/>"
    "<div id='describe-box' class='stack' style='display:none'>"
    "<div class='field'><label>自动生成的摘要（可直接修改）</label>"
    "<input name='summary'/></div>"
    "<div class='field'><label>自动生成的关键词（逗号分隔，可直接修改）</label>"
    "<input name='keywords'/></div>"
    "<div class='field'><label>自动生成的细节说明（可直接修改）</label>"
    "<textarea name='details' rows='4'></textarea></div>"
    "</div></form>"
    "<p class='muted' id='describe-status'>选择图片后会自动识别并生成描述，不需要手填。</p>"
    "<p class='muted' id='upload-msg'></p></section>"
    "<section><h2>按平台召回素材</h2>"
    "<p class='muted'>按《四平台推荐风格与方式报告》里各平台要求的画面位次逐格挑图。"
    "某一格没有合适素材时会直接提示需要补拍，不会拿别的图凑数。</p>"
    "<form id='recall'><div class='row'>"
    "<div class='field'><label>目标平台</label>"
    "<select name='platform' id='platform-select'></select></div>"
    "<div class='field'><label>每格返回条数</label>"
    "<input name='top_k' type='number' value='1' min='1' max='5'/></div>"
    "<div class='field'><label>补充关键词（选填）</label>"
    "<input name='query' placeholder='例如 壳体 配重 管件'/></div>"
    "<div class='field'><label>操作</label>"
    "<button type='submit'>执行召回</button></div>"
    "</div></form>"
    "<div class='hint' id='platform-summary'></div>"
    "<div class='hint' id='recall-shortfall'></div>"
    "<div id='recall-result'></div>"
    "<div class='export-bar'>"
    "<button type='button' id='export-btn' disabled>导出这一帖的素材</button>"
    "<button type='button' id='export-open' style='display:none'>复制导出文件夹路径</button>"
    "<span class='hint' id='export-status'>请先执行召回，再导出（导出后这些素材进入 15 天冷却）。</span></div>"
    "<div class='caption-box' id='caption-box' style='display:none'>"
    "<div id='caption-head'></div>"
    "<div class='field'><label>补充要求（选填，例如“突出机床床身与导轨面”）</label>"
    "<input id='caption-note'/></div>"
    "<div><button type='button' id='caption-btn'>按该平台模式生成短文</button> "
    "<button type='button' id='caption-copy'>复制短文</button></div>"
    "<div class='hint' id='caption-status'></div>"
    "<div class='field'><label>短文正文（可编辑，改完直接复制发布）</label>"
    "<textarea id='caption-text' rows='10'></textarea></div>"
    "<ul class='checks' id='caption-checks'></ul>"
    "<div class='field' id='caption-comment-wrap' style='display:none'>"
    "<label>第一条评论（链接放这里，正文不放）</label>"
    "<textarea id='caption-comment' rows='2'></textarea></div>"
    "<div class='field' id='caption-en-wrap' style='display:none'>"
    "<label>英语母版（供复用与审校）</label>"
    "<textarea id='caption-en' rows='6'></textarea></div>"
    "</div></section>"
    "<section><h2>素材清单</h2><div class='grid' id='assets'></div></section>"
    "</main><script>"
    "function esc(v){var d=document.createElement('div');d.textContent=v==null?'':String(v);"
    "return d.innerHTML;}"
    "var visionTicket='';"
    "var categories=[];"
    "function optionHtml(value,label,selected){return \"<option value='\"+esc(value)+\"'\""
    "+((String(value)===String(selected))?\" selected\":'')+\">\"+esc(label)+\"</option>\";}"
    "function fillProcessOptions(selected){var sel=document.getElementById('process-select');"
    "if(!sel){return;}var seen=[];"
    "categories.forEach(function(c){if(seen.indexOf(c.process)<0){seen.push(c.process);}});"
    "sel.innerHTML=seen.map(function(p){return optionHtml(p,p,selected||seen[0]);}).join('');"
    "fillSubOptions(sel.value,'');}"
    "function fillSubOptions(process,selected){var sel=document.getElementById('sub-process-select');"
    "if(!sel){return;}var subs=categories.filter(function(c){return c.process===process;});"
    "sel.innerHTML=subs.length?subs.map(function(c){var v=c.sub_process;"
    "return optionHtml(v,v?(v+'（'+c.count+'）'):'（无子类）',selected);}).join('')"
    ":optionHtml('','（无子类）','');}"
    "function applyCategories(list){if(!list||!list.length){return;}"
    "categories=list;var sel=document.getElementById('process-select');"
    "if(sel&&!sel.options.length){fillProcessOptions('');}}"
    "function syncSubOptions(){var p=document.getElementById('process-select');"
    "if(p){fillSubOptions(p.value,'');}}"
    "function chosenProcess(){var p=document.getElementById('process-select');return p?p.value:'';}"
    "function chosenSub(){var s=document.getElementById('sub-process-select');return s?s.value:'';}"
    "var platforms=[];"
    "var lastRecall=null;"
    "var lastCaption=null;"
    "var exportSlots=[];"
    "function renderExportButton(d){var btn=document.getElementById('export-btn');"
    "if(!d.platform||!d.picks||!d.picks.length){btn.disabled=true;"
    "document.getElementById('export-status').textContent='请先执行召回，再导出。';return;}"
    "exportSlots=[];(d.slots||[]).forEach(function(s){"
    "(s.picks||[]).forEach(function(p){"
    "exportSlots.push({order:s.order,role:s.role,asset_id:p.asset_id});});});"
    "btn.disabled=exportSlots.length===0;"
    "document.getElementById('export-status').textContent="
    "'共 '+exportSlots.length+' 条素材可导出（按位次顺序）。'"
    "+(lastCaption?'点导出会连文案一起写进文件夹。':'生成短文后再导出，文案会一起写进文件夹。');}"
    "function renderCaptionPanel(d){var box=document.getElementById('caption-box');"
    "if(!d.platform||!d.picks||!d.picks.length){box.style.display='none';return;}"
    "box.style.display='grid';"
    "var key=d.platform.key;var spec=(captionSpecs.filter(function(s){return s.key===key;})[0])||{};"
    "document.getElementById('caption-head').innerHTML=\"<h3>\"+esc(d.platform.name)"
    "+\" 短文（按报告模式生成）</h3>\"+(spec.structure_text?(\"<p class='muted'>结构：\""
    "+esc(spec.structure_text)+\" ｜ 长度：\"+esc(spec.length_text)+\" ｜ 标签 \"+spec.hashtag_min"
    "+\"–\"+spec.hashtag_max+\" 个 ｜ \"+esc(spec.link_rule)+\"</p>\"):'');"
    "document.getElementById('caption-text').value='';"
    "document.getElementById('caption-checks').innerHTML='';"
    "document.getElementById('caption-comment-wrap').style.display='none';"
    "document.getElementById('caption-en-wrap').style.display='none';"
    "document.getElementById('caption-status').textContent="
    "'素材已就绪（'+d.picks.length+' 张），点上面的按钮生成。';}"
    "var captionSpecs=[];"
    "function applyCaptionSpecs(list){if(list&&list.length){captionSpecs=list;}}"
    "function applyPlatforms(list){if(!list||!list.length){return;}platforms=list;"
    "var sel=document.getElementById('platform-select');if(!sel||sel.options.length){return;}"
    "sel.innerHTML=list.map(function(p){"
    "return optionHtml(p.key,p.name+'（'+p.priority+'）',list[0].key);}).join('');}"
    "function setGate(on,text,bad){var b=document.getElementById('upload-btn');"
    "if(b){b.disabled=!on;}"
    "var g=document.getElementById('upload-gate');"
    "if(g){g.className='hint'+(bad?' bad':(on?' ok':''));g.textContent=text;}"
    "if(!on){visionTicket='';var t=document.getElementById('vision-ticket');"
    "if(t){t.value='';}}"
    "}"
    "function assetCard(a,extra){var tag='tag'+(a.status==='new'?' new':(a.status==='cooling'?' cooling':''));"
    "return \"<div class='asset'><h3>\"+esc(a.file_name)+\"</h3><span class='\"+tag+\"'>\"+esc(a.status_label)"
    "+\"</span><p class='muted'>\"+esc(a.category)+\"</p><p>\"+esc(a.summary)+\"</p>\"+(extra||'')+\"</div>\";}"
    "function refresh(){fetch('/api/state').then(function(r){return r.json();}).then(function(d){"
    "applyCategories(d.categories);"
    "applyPlatforms(d.platforms);"
    "applyCaptionSpecs(d.caption_specs);"
    "var c=d.capacity;var b=document.getElementById('banner');"
    "b.className='banner '+(c.alert==='red'?'red':'ok');"
    "b.textContent=(c.alert==='red'?('红色预警：可召回容量仅 '+c.available+' 张，低于阈值 '"
    "+c.threshold+' 张（约一周用量），请立即补充实拍素材。'):('容量正常：可召回 '+c.available+' 张 / 共 '"
    "+c.total+' 张（冷却中 '+c.cooling+' 张，新图 '+c.new+' 张）。'));"
    "var cards=[['可召回',c.available],['冷却中',c.cooling],['新图',c.new],['老图',c.legacy],"
    "['总量',c.total],['阈值',c.threshold]];"
    "document.getElementById('cards').innerHTML=cards.map(function(p){"
    "return \"<div class='card'>\"+p[0]+\"<b>\"+p[1]+\"</b></div>\";}).join('');"
    "document.getElementById('assets').innerHTML=d.assets.map(function(a){"
    "return assetCard(a,\"<button onclick=\\\"markUsed('\"+a.asset_id+\"')\\\">标记已用于内容</button>\");"
    "}).join('');});}"
    "function markUsed(id){fetch('/api/usage',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({asset_id:id})}).then(refresh);}"
    "document.getElementById('upload').addEventListener('submit',function(e){e.preventDefault();"
    "if(!visionTicket){document.getElementById('upload-msg').textContent="
    "'请先等识别完成：识别成功后才能入库。';return;}"
    "fetch('/api/assets',{method:'POST',body:new FormData(e.target)}).then(function(r){return r.json();})"
    ".then(function(d){document.getElementById('upload-msg').textContent="
    "d.ok?('已入库：'+d.file_name+'（'+d.asset_id+'）'):('失败：'+d.error);"
    "if(d.ok){var f=document.getElementById('upload');f.reset();"
    "document.getElementById('describe-box').style.display='none';"
    "document.getElementById('vision-ticket').value='';"
    "document.getElementById('describe-status').textContent="
    "'选择图片后会自动识别并生成描述，不需要手填。';"
    "setGate(false,'请先选择图片，识别成功后按钮才会变亮。',false);refresh();}});});"
    # 先查重再识别：重复素材在浏览器里就能算出来，连上传都省掉，更不会产生模型费用
    "function sha256Hex(file){"
    "if(!window.crypto||!crypto.subtle||!file.arrayBuffer){return Promise.resolve('');}"
    "return file.arrayBuffer()"
    ".then(function(buf){return crypto.subtle.digest('SHA-256',buf);})"
    ".then(function(hash){var bytes=new Uint8Array(hash);var out='';"
    "for(var i=0;i<bytes.length;i++){out+=('00'+bytes[i].toString(16)).slice(-2);}return out;})"
    ".catch(function(){return '';});}"
    "function markDuplicate(message){"
    "document.getElementById('describe-box').style.display='none';"
    "document.getElementById('describe-status').textContent="
    "'已跳过识别（内容重复，不产生模型费用），换一张再试。';"
    "setGate(false,message,true);}"
    "function describeFile(){var f=document.getElementById('upload');"
    "var file=f.querySelector('input[name=file]').files[0];"
    "document.getElementById('vision-ticket').value='';"
    "document.getElementById('upload-msg').textContent='';"
    "if(!file){document.getElementById('describe-box').style.display='none';"
    "setGate(false,'请先选择图片，识别成功后按钮才会变亮。',false);return;}"
    "setGate(false,'正在校验是否重复…',false);"
    "document.getElementById('describe-status').textContent="
    "'先做内容查重：重复的素材不必再花钱识别。';"
    "sha256Hex(file).then(function(hash){"
    "if(!hash){return false;}"
    "return fetch('/api/precheck',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({hash:hash,size:file.size})})"
    ".then(function(r){return r.json();}).then(function(d){"
    "if(d&&d.duplicate){markDuplicate(d.message);return true;}return false;});"
    "}).then(function(handled){if(!handled){runDescribe(file);}})"
    ".catch(function(){runDescribe(file);});}"
    "function runDescribe(file){var f=document.getElementById('upload');"
    "setGate(false,'正在识别…识别完成前不能入库。',false);"
    "var fd=new FormData();fd.append('file',file);"
    "fd.append('process',chosenProcess());fd.append('sub_process',chosenSub());"
    "document.getElementById('describe-status').textContent='正在识别并生成描述，请稍等…';"
    "fetch('/api/describe',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){"
    "if(d.duplicate){markDuplicate(d.message);return;}"
    "if(!d.ok){document.getElementById('describe-status').textContent='自动生成失败：'+d.error;"
    "setGate(false,'视觉识别失败，无法入库。',true);return;}"
    "document.getElementById('describe-box').style.display='block';"
    "f.querySelector('input[name=summary]').value=d.summary||'';"
    "f.querySelector('input[name=keywords]').value=(d.keywords||[]).join(', ');"
    "f.querySelector('textarea[name=details]').value=d.details||'';"
    "var msg=d.source==='vision'?'已用视觉模型自动生成描述':"
    "'已按文件名与规格信息自动生成（未配置视觉模型，画面内容待补充）';"
    "if(d.vision_usage){msg+='。本次消耗 '+d.vision_usage;}"
    "if(d.warnings&&d.warnings.length){msg+='。提示：'+d.warnings.join('；');}"
    "document.getElementById('describe-status').textContent=msg;"
    "if(d.process){var ps=document.getElementById('process-select');"
    "if(ps&&ps.value!==d.process){ps.value=d.process;}"
    "fillSubOptions(d.process,d.sub_process||'');}"
    "var note=document.getElementById('category-note');"
    "if(note){var how=d.category_source;"
    "note.textContent=how==='vision'?('品类由图片自动识别：'+d.process+(d.sub_process?('/'+d.sub_process):'')+'（可手动改）')"
    ":how==='keyword'?('视觉模型没给出品类，按识别出的关键词匹配为：'+d.process+(d.sub_process?('/'+d.sub_process):'')+'，请确认后入库')"
    ":how==='manual'?('沿用了你已选的品类：'+d.process+(d.sub_process?('/'+d.sub_process):''))"
    ":('没能判断出品类，请在上方手动选择后再入库。');"
    "note.className='hint'+(how==='none'?' bad':'');}"
    "if(d.vision_ticket){document.getElementById('vision-ticket').value=d.vision_ticket;"
    "visionTicket=d.vision_ticket;"
    "setGate(true,'视觉识别已完成，可以入库。',false);}"
    "else{setGate(false,d.vision_required===false?('视觉识别未完成，但当前已关闭入库校验，可直接入库。'):"
    "('视觉识别未完成，不能入库。'+(d.vision_error?('原因：'+d.vision_error+'　'):'')"
    "+'点上面的「视觉模型自检」看详细原因。'),"
    "d.vision_required!==false);}})"
    ".catch(function(){document.getElementById('describe-status').textContent='自动生成失败，请重试。';"
    "setGate(false,'网络异常，视觉识别未完成，不能入库。',true);});}"
    "document.getElementById('upload').querySelector('input[name=file]')"
    ".addEventListener('change',describeFile);"
    "var processSelect=document.getElementById('process-select');"
    "if(processSelect){processSelect.addEventListener('change',function(){"
    "syncSubOptions();var note=document.getElementById('category-note');"
    "if(note&&!visionTicket){note.textContent='';}});}"
    "var subSelect=document.getElementById('sub-process-select');"
    "if(subSelect){subSelect.addEventListener('change',function(){"
    "var note=document.getElementById('category-note');"
    "if(note&&!visionTicket){note.textContent='';}});}"
    "document.getElementById('vision-check-btn').addEventListener('click',function(){"
    "var out=document.getElementById('vision-check-result');"
    "out.className='hint';out.textContent='正在自检（会真实调用一次模型，约需几秒）…';"
    "fetch('/api/vision-check',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:'{}'}).then(function(r){return r.json();}).then(function(d){"
    "out.className='hint'+(d.ok?' ok':' bad');"
    "out.textContent=(d.ok?'✓ 视觉模型可用：':'✗ 视觉模型不可用（阶段 '+(d.stage||'?')+'）：')"
    "+d.message+(d.hint?('　建议：'+d.hint):'')"
    "+'　［模型 '+d.model+'｜地址 '+d.endpoint+'｜Key '"
    "+(d.key_configured?'已配置':'未配置')+'｜超时 '+d.timeout_s+' 秒］';});"
    "});"
    "document.getElementById('caption-btn').addEventListener('click',function(){"
    "if(!lastRecall||!lastRecall.platform){return;}"
    "var status=document.getElementById('caption-status');"
    "status.textContent='正在生成，请稍等…';"
    "fetch('/api/caption',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({platform:lastRecall.platform.key,"
    "asset_ids:lastRecall.picks.map(function(p){return p.asset_id;}),"
    "extra_note:document.getElementById('caption-note').value})})"
    ".then(function(r){return r.json();}).then(function(d){"
    "if(!d.ok){status.textContent='生成失败：'+d.error;return;}"
    "lastCaption=d;"
    "document.getElementById('caption-text').value=d.full_text||d.text;"
    "document.getElementById('caption-checks').innerHTML=(d.checks||[]).map(function(c){"
    "return \"<li class='\"+(c.ok?'ok':'bad')+\"'>\"+(c.ok?'✓':'！')+esc(c.name)+'：'"
    "+esc(c.detail)+'</li>';}).join('');"
    "if(d.text_en){document.getElementById('caption-en').value=d.text_en;"
    "document.getElementById('caption-en-wrap').style.display='flex';}"
    "if(d.first_comment){document.getElementById('caption-comment').value=d.first_comment;"
    "document.getElementById('caption-comment-wrap').style.display='flex';}"
    "status.textContent='已按 '+d.platform_name+' 模式生成（正文 '+d.char_count+' 字符，基于 '"
    "+d.material_count+' 张素材）。'+(d.warnings&&d.warnings.length?('提示：'+d.warnings.join('；')):'');"
    "}).catch(function(){status.textContent='生成失败，请重试。';});});"
    "document.getElementById('caption-copy').addEventListener('click',function(){"
    "var text=document.getElementById('caption-text');"
    "if(!text.value){return;}text.select();"
    "try{navigator.clipboard.writeText(text.value);}catch(e){document.execCommand('copy');}"
    "document.getElementById('caption-status').textContent='短文已复制到剪贴板。';});"
    "document.getElementById('recall').addEventListener('submit',function(e){e.preventDefault();"
    "var f=new FormData(e.target);"
    "fetch('/api/recall',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({platform:f.get('platform'),query:f.get('query'),"
    "top_k:Number(f.get('top_k'))})})"
    ".then(function(r){return r.json();}).then(function(d){"
    "var info=document.getElementById('platform-summary');"
    "if(d.platform){info.textContent=d.platform.name+' ｜ '+d.platform.priority+' ｜ 画幅 '+"
    "d.platform.aspect+' ｜ '+d.platform.shot_count+' ｜ '+d.platform.form+"
    "(d.platform.contract_note?('　'+d.platform.contract_note):'');"
    "info.className='hint'+(d.platform.contract_note?' bad':'');}else{info.textContent='';}"
    "if(!d.slots||!d.slots.length){document.getElementById('recall-result').innerHTML="
    "d.picks.map(function(p){return photoHtml(p);}).join('')"
    "||\"<p class='muted'>没有可召回素材（可能全部处于冷却期）。</p>\";return;}"
    "document.getElementById('recall-result').innerHTML=d.slots.map(function(s){"
    "var body=s.picks.length?(\"<div class='picks'>\"+s.picks.map(function(p){return photoHtml(p);}).join('')+\"</div>\")"
    ":(s.blocked_by_cooldown"
    "?\"<p class='muted'>这一格有符合的素材，但都在 15 天冷却期内，暂时不能用。"
    "可以先换别的图，或等冷却结束。</p>\""
    ":\"<p class='muted'>这一格还没有合适的素材，建议按这个位次补拍。</p>\");"
    # 匹配档次：让人一眼看出这张是"精准匹配"还是"放宽补位"，别把凑数的当成正牌货
    "var lv=s.level_label?(\"<p class='hint\"+(s.relaxed?' bad':'')+\"'>匹配：\""
    "+esc(s.level_label)+\"（精准候选 \"+s.matched+\" 张 / 全库 \"+s.available+\" 张）\""
    "+(s.relaxed?(\"　—— 这一格没有精准素材，这张是放宽补位，建议补拍。\"):'')+\"</p>\"):'';"
    "return \"<div class='slot'><h3>\"+esc(s.role)+\"</h3>\""
    "+(s.note?(\"<p class='muted'>\"+esc(s.note)+\"</p>\"):'')+lv+body+'</div>';}).join('');"
    "var box=document.getElementById('recall-shortfall');"
    "if(d.shortfall>0){box.className='hint bad';"
    "box.textContent='⚠ 本次只召回到 '+d.count+' / '+d.requested"
    "+' 张：素材不够（可能都在冷却期，或这个题材本来就少），建议补拍。';}"
    "else{box.className='hint ok';box.textContent='✓ 已按要求召回 '+d.count+' 张。';}"
    "lastRecall=d;renderCaptionPanel(d);renderExportButton(d);});});"
    "document.getElementById('export-btn').addEventListener('click',function(){"
    "if(!lastRecall||!lastRecall.platform||!exportSlots.length){return;}"
    "var status=document.getElementById('export-status');"
    "status.textContent='正在导出，请稍等…';"
    "fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({platform:lastRecall.platform.key,slots:exportSlots,"
    "caption:lastCaption?Object.assign({},lastCaption,"
    "{text:document.getElementById('caption-text').value,hashtags:[]}):null})})"
    ".then(function(r){return r.json();}).then(function(d){"
    "if(!d.ok){status.textContent='导出失败：'+d.error;return;}"
    "status.textContent='已导出 '+d.file_count+' 个文件到：'+d.directory"
    "+((d.used&&d.used.length)?('，'+d.used.length+' 张素材进入 '+d.cooldown_days+' 天冷却'):'')"
    "+(d.missing&&d.missing.length?('（有 '+d.missing.length+' 条素材没找到文件，详见说明.txt）'):'');"
    "status.className='hint'+(d.missing&&d.missing.length?' bad':' ok');"
    "document.getElementById('export-open').style.display='inline-block';"
    "document.getElementById('export-open').dataset.path=d.directory;"
    "refresh();"
    "}).catch(function(){status.textContent='导出失败，请重试。';});});"
    "document.getElementById('export-open').addEventListener('click',function(){"
    "var path=this.dataset.path||'';"
    "try{navigator.clipboard.writeText(path);}catch(e){}"
    "document.getElementById('export-status').textContent='路径已复制：'+path;});"
    "function photoHtml(p){return \"<div class='asset'><h3>\"+esc(p.file_name)+\"</h3>\""
    "+(p.is_video"
    "?(\"<video class='thumb' controls preload='metadata' src='/api/asset-image?asset_id=\""
    "+encodeURIComponent(p.asset_id)+\"'></video>\")"
    ":(\"<img class='thumb' loading='lazy' alt='' src='/api/asset-image?asset_id=\""
    "+encodeURIComponent(p.asset_id)+\"'/>\"))"
    "+\"<span class='tag\"+(p.is_new?' new':'')+\"'>\"+esc(p.category)+\"</span>\""
    "+\"<p class='muted'>权重 \"+p.weight+\" ｜ 相似度 \"+p.similarity+\"</p>\""
    "+\"<p>\"+esc(p.summary)+\"</p></div>\";}"
    "refresh();</script></body></html>"
)


class VisionRequiredError(ValueError):
    """入库许可未满足：这张图还没有完成视觉识别。"""


def _is_within(path: Path, root: Path) -> bool:
    """路径必须落在素材根目录内——防止描述头部里的路径把请求带到库外。"""
    try:
        path.resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    return True


#: 把开关写成这些值视为"关闭入库校验"
_FALSY_WORDS = frozenset({"0", "false", "no", "off", "n", "否"})


def resolve_require_vision(root: str | Path | None = None) -> bool:
    """入库是否强制要求先完成视觉识别。

    默认开启（``config.REQUIRE_VISION_BEFORE_INGEST``）；应急时可在 ``.env`` 或
    环境变量里设 ``PULSE_MEDIA_REQUIRE_VISION=0`` 关闭。
    """
    raw = os.environ.get("PULSE_MEDIA_REQUIRE_VISION")
    if raw is None and root is not None:
        raw = read_env_file(Path(root) / ".env").get("PULSE_MEDIA_REQUIRE_VISION")
    if raw is None or not str(raw).strip():
        return REQUIRE_VISION_BEFORE_INGEST
    return str(raw).strip().lower() not in _FALSY_WORDS


class MediaConsoleApp:
    """控制台的后端用例层（可脱离 HTTP 单独测试）。"""

    def __init__(
        self,
        *,
        rag_root: str | Path,
        media_root: str | Path,
        ledger_path: str | Path,
        registry_path: str | Path | None = None,
        config: RecallConfig | None = None,
        describer: Any | None = None,
        env_root: str | Path | None = None,
        require_vision: bool | None = None,
        caption_client: Any | None = None,
        export_root: str | Path | None = None,
    ) -> None:
        self.config = config or RecallConfig()
        self.rag_root = Path(rag_root)
        self.media_root = Path(media_root)
        self.registry = MediaRegistry(registry_path or self.rag_root / DEFAULT_REGISTRY_NAME)
        self.ledger = RecallLedger(ledger_path)
        self.policy = RecallPolicy(self.ledger, self.config)
        # 描述器：优先视觉模型；未配置或调用失败时退回基础信息描述
        root = Path(env_root) if env_root else None
        #: 可选品类目录：来自 RAG 库现有目录，决定下拉框与模型的可选范围
        self._catalog: CategoryCatalog = load_categories(self.rag_root)
        #: 视觉模型配置：描述生成与短文生成共用同一条通道
        self.vision_config = vision_config_from_env(root)
        #: 注入的模型客户端（测试用；为空则走真实 HTTP），短文与视频识别共用
        self.caption_client = caption_client
        self._vision_describer: VisionDescriber | None = None
        if describer is None and self.vision_config.enabled:
            self._vision_describer = VisionDescriber(
                self.vision_config, catalog=self._catalog, client=caption_client
            )
        self.describer = (
            describer
            or (
                self._vision_describer.describe
                if self._vision_describer is not None
                else build_describer(root, catalog=self._catalog, config=self.vision_config)
            )
        )
        #: 导出落地目录：默认放在素材库旁边的"Pulse导出"（老板容易找到）
        self.export_root = self._resolve_export_root(root) if export_root is None else Path(export_root)
        # 入库许可：默认"必须先完成视觉识别"，可用环境变量临时关闭
        self.require_vision = (
            resolve_require_vision(root) if require_vision is None else require_vision
        )
        self._vision_tickets: dict[str, dict[str, Any]] = {}
        self.ingestor = MediaIngestor(
            rag_root=self.rag_root,
            media_root=self.media_root,
            registry=self.registry,
            brand=self.config.brand,
        )

    def assets(self) -> list[MediaAsset]:
        return load_catalog(self.rag_root, self.registry, brand=self.config.brand)

    @property
    def catalog(self) -> CategoryCatalog:
        return self._catalog

    def _resolve_export_root(self, root: Path | None) -> Path:
        """导出目录：优先 PULSE_EXPORT_DIR，否则放在素材库旁边的"Pulse导出"。"""
        raw = os.environ.get("PULSE_EXPORT_DIR")
        if raw is None and root is not None:
            raw = read_env_file(Path(root) / ".env").get("PULSE_EXPORT_DIR")
        if raw and str(raw).strip():
            return Path(str(raw).strip())
        return self.media_root.resolve().parent / "Pulse导出"

    def refresh_catalog(self) -> CategoryCatalog:
        """重新扫描品类目录（入库后调用，让下拉框跟上新目录）。"""
        self._catalog = load_categories(self.rag_root)
        return self._catalog

    def state(self, *, now: datetime | None = None) -> dict[str, Any]:
        """素材清单 + 容量报告（含每张图的冷却状态）。"""
        reference = now or datetime.now(timezone.utc)
        assets = self.assets()
        capacity = self.policy.capacity(assets, now=reference)
        items: list[dict[str, Any]] = []
        for asset in assets:
            remaining = self.policy.cooldown_remaining(asset, now=reference)
            days_left = 0
            if remaining.total_seconds() > 0:
                days_left = max(1, remaining.days + (1 if remaining.seconds else 0))
                status, label = "cooling", f"冷却中 · 剩 {days_left} 天"
            elif asset.is_legacy:
                status, label = "legacy", "可用（老图）"
            else:
                status, label = "new", "可用（新图，优先召回）"
            items.append(
                {
                    "asset_id": asset.asset_id,
                    "file_name": asset.file_name,
                    "category": asset.category,
                    "summary": asset.summary,
                    "keywords": list(asset.keywords),
                    "status": status,
                    "status_label": label,
                    "is_new": not asset.is_legacy,
                    "added_at": asset.added_at.isoformat() if asset.added_at else None,
                    "cooldown_days_left": days_left,
                }
            )
        return {
            "brand": capacity.brand,
            # 版本号给启动器用：它据此判断"端口上那个控制台是不是同一版代码"，
            # 避免双击两次留下两个监听同一端口的旧进程（2026-09-15 的真实事故）。
            "version": __version__,
            "config": {
                "cooldown_days": self.config.cooldown_days,
                "capacity_red_threshold": self.config.capacity_red_threshold,
                "freshness_window_days": self.config.freshness_window_days,
                "freshness_boost": self.config.freshness_boost,
            },
            "capacity": {
                "total": capacity.total,
                "available": capacity.available,
                "cooling": capacity.cooling,
                "new": capacity.new,
                "legacy": capacity.legacy,
                "videos": capacity.videos,
                "threshold": capacity.threshold,
                "alert": capacity.alert,
            },
            "categories": self._catalog.as_payload(),
            "platforms": platform_choices(),
            "caption_specs": [spec.as_payload() for spec in CAPTION_SPECS.values()],
            "assets": items,
        }

    def upload(
        self,
        *,
        file_name: str,
        data: bytes,
        process: str,
        sub_process: str,
        keywords: tuple[str, ...] = (),
        summary: str = "",
        details: str = "",
        vision_ticket: str | None = None,
    ) -> dict[str, Any]:
        """上传一张实拍图并入库；未提供描述时自动生成。

        入库许可：默认要求描述来自视觉识别。页面提交的描述必须附带识别凭据，
        服务端会核对凭据与图片内容哈希是否一致；不附凭据时退回服务端自行识别。
        """
        generated: dict[str, Any] | None = None
        if not (summary.strip() or details.strip() or keywords):
            generated = self.describe(
                file_name=file_name, data=data, process=process, sub_process=sub_process
            )
            if self.require_vision and generated.get("source") != "vision":
                raise VisionRequiredError(
                    "视觉识别未完成，无法入库。请检查 .env 里的视觉模型配置后重试；"
                    "确需应急放行，可设置 PULSE_MEDIA_REQUIRE_VISION=0。"
                )
            summary = str(generated.get("summary") or "")
            details = str(generated.get("details") or "")
            keywords = tuple(generated.get("keywords") or ())
        elif self.require_vision:
            self._assert_vision_ticket(vision_ticket, data, process=process, sub_process=sub_process)
        asset = self.ingestor.add_media(
            file_name=file_name,
            process=process,
            sub_process=sub_process,
            data=data,
            keywords=keywords,
            summary=summary,
            details=details,
        )
        self.refresh_catalog()
        return {
            "ok": True,
            "asset_id": asset.asset_id,
            "file_name": asset.file_name,
            "category": asset.category,
            "description_source": generated.get("source") if generated else "manual",
            "warnings": list(generated.get("warnings") or []) if generated else [],
        }

    # ---------- 入库许可 ----------
    def _content_hash(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _issue_vision_ticket(
        self, *, file_name: str, data: bytes, process: str, sub_process: str
    ) -> str:
        """识别成功后签发一次性凭据，供随后的入库请求证明"这张图已识别"。"""
        self._purge_expired_tickets()
        token = secrets.token_urlsafe(24)
        self._vision_tickets[token] = {
            "content_hash": self._content_hash(data),
            "file_name": file_name,
            "process": process,
            "sub_process": sub_process,
            "issued_at": datetime.now(timezone.utc),
        }
        return token

    def _purge_expired_tickets(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            token
            for token, record in self._vision_tickets.items()
            if (now - record["issued_at"]).total_seconds() > VISION_TICKET_TTL_SECONDS
        ]
        for token in expired:
            self._vision_tickets.pop(token, None)

    def _assert_vision_ticket(
        self, ticket: str | None, data: bytes, *, process: str, sub_process: str
    ) -> None:
        key = (ticket or "").strip()
        record = self._vision_tickets.get(key)
        if record is None:
            raise VisionRequiredError(
                "这张图还没有完成视觉识别，不能入库。请等页面识别成功、"
                "「上传并入库」按钮变亮后再提交。"
            )
        if (datetime.now(timezone.utc) - record["issued_at"]).total_seconds() > VISION_TICKET_TTL_SECONDS:
            self._vision_tickets.pop(key, None)
            raise VisionRequiredError("识别凭据已过期，请重新选择图片完成识别。")
        if record["content_hash"] != self._content_hash(data):
            raise VisionRequiredError(
                "识别凭据与当前图片不一致（图片内容已变），请重新选择图片完成识别。"
            )

    def describe(
        self, *, file_name: str, data: bytes, process: str, sub_process: str
    ) -> dict[str, Any]:
        """自动生成素材描述：视觉模型优先，未配置或失败时退回基础信息。

        图片直接送模型；**视频接口不支持**（实测 ``input_video`` 被拒），
        所以先用 ffmpeg 抽成若干静帧、再当"一段连续画面"送过去。

        只有视觉识别成功（``source == "vision"``）才签发入库许可凭据。
        """
        video = is_video_name(file_name)
        frames: list[bytes] = []
        video_note = ""
        # 先查重再识别：重复内容在入库时也会被拒，但那时识别费已经花掉了。
        # 这条检查不调用模型，因此命中时零成本。
        duplicate = self.find_duplicate(
            content_hash=hashlib.sha256(data).hexdigest(), size=len(data)
        )
        if duplicate is not None:
            return {
                "ok": True,
                "duplicate": True,
                "existing": duplicate,
                "message": _duplicate_message(duplicate),
                "summary": "",
                "details": "",
                "keywords": [],
                "source": "duplicate",
                "warnings": (),
                "process": "",
                "sub_process": "",
                "category_source": "none",
                "vision_required": self.require_vision,
                "vision_error": "",
                "vision_usage": "",
                "vision_ticket": "",
            }
        try:
            if video:
                frames, info = extract_frames_from_bytes(
                    data,
                    file_name,
                    count=self.vision_config.video_frames,
                    width=self.vision_config.video_frame_width,
                )
                if info is not None:
                    video_note = (
                        f"视频时长约 {info.duration_s:.1f} 秒，原始分辨率 {info.size_text}，"
                        f"已抽 {len(frames)} 帧送识别。"
                    )
                frames_describer = self._vision_describer or VisionDescriber(
                    self.vision_config, catalog=self._catalog, client=self.caption_client
                )
                description = frames_describer.describe_frames(
                    frames,
                    file_name=file_name,
                    process=process,
                    sub_process=sub_process,
                    duration_s=info.duration_s if info else 0.0,
                    size_text=info.size_text if info else "",
                )
                vision_note = frames_describer.usage_text()
            else:
                description = self.describer(
                    data, file_name=file_name, process=process, sub_process=sub_process
                )
                vision_note = (
                    self._vision_describer.usage_text()
                    if self._vision_describer is not None
                    else ""
                )
            vision_error = ""
        except Exception as exc:  # 网络 / 额度 / 解析失败都不应阻断入库
            description = replace(
                heuristic_describe(
                    frames[0] if frames else data,
                    file_name=file_name,
                    process=process,
                    sub_process=sub_process,
                ),
                warnings=(
                    f"视觉模型调用失败，已退回基础信息描述：{exc}",
                    "画面内容待人工补充。",
                ),
            )
            vision_error = str(exc)
            vision_note = ""
        if video_note:
            description = replace(
                description, warnings=tuple(description.warnings) + (video_note,)
            )
        vision_ok = description.source == "vision"
        if not vision_ok and not vision_error and not self.vision_config.enabled:
            vision_error = "未配置 PULSE_VISION_API_KEY（见仓库根 .env）"
        # 品类：模型/描述器给了就用，没给则按识别出的关键词兜底，
        # 再不行——保留调用方已经选好的品类，最后才留空交给人工。
        resolved_process = (description.process or "").strip()
        resolved_sub = (description.sub_process or "").strip()
        category_source = description.category_source or ""
        if not resolved_process:
            resolved_process, resolved_sub, category_source = resolve_category(
                "",
                "",
                text=f"{description.summary} {description.details}",
                keywords=description.keywords,
                catalog=self._catalog,
            )
        if category_source in ("", "none") and self._catalog.has(process, sub_process):
            resolved_process, resolved_sub, category_source = process, sub_process, "manual"
        elif not self._catalog and not resolved_process and process:
            # 库还是空的（没有任何既有品类）时不设限，采信调用方的选择
            resolved_process, resolved_sub, category_source = process, sub_process, "manual"
        return {
            "ok": True,
            "summary": description.summary,
            "details": description.details,
            "keywords": list(description.keywords),
            "source": description.source,
            "warnings": list(description.warnings),
            "process": resolved_process,
            "sub_process": resolved_sub,
            "category_source": category_source,
            "vision_required": self.require_vision,
            "duplicate": False,
            # 视觉识别失败时把**真实原因**带出去：只报"请检查 .env"会把人带偏
            "vision_error": vision_error,
            # 本次识别用掉多少 token（含思考），让成本可见
            "vision_usage": vision_note,
            "vision_ticket": (
                self._issue_vision_ticket(
                    file_name=file_name, data=data, process=process, sub_process=sub_process
                )
                if vision_ok
                else ""
            ),
        }

    def mark_used(self, *, asset_id: str, content_id: str | None = None) -> dict[str, Any]:
        """标记素材已实际进入内容 / 发布（触发冷却）。"""
        inserted = self.ledger.mark_used(
            asset_id, brand=self.config.brand, content_id=content_id, event="used"
        )
        return {
            "ok": True,
            "duplicate": not inserted,
            "asset_id": asset_id,
            "cooldown_days": self.config.cooldown_days,
        }

    def mark_exported_used(
        self, asset_ids: tuple[str, ...], *, content_id: str
    ) -> list[str]:
        """把"真的进了导出包"的素材记入台账（触发冷却），返回本次新计入的素材 ID。

        导出即视为这一帖进入发布（媒体库契约 §2.1），所以导出后不需要再人工点
        "标记已用于内容"。没复制成功的素材不会出现在 ``asset_ids`` 里，不会被误计。
        """
        marked: list[str] = []
        for asset_id in asset_ids:
            if self.ledger.mark_used(
                asset_id, brand=self.config.brand, content_id=content_id, event="used"
            ):
                marked.append(asset_id)
        return marked

    @staticmethod
    def _pick_payload(pick: Any) -> dict[str, Any]:
        return {
            "asset_id": pick.asset_id,
            "file_name": pick.file_name,
            "category": pick.category,
            "summary": pick.summary,
            "similarity": pick.similarity,
            "weight": pick.weight,
            "is_new": pick.is_new,
            "is_video": str(pick.file_name).lower().endswith(
                (".mp4", ".mov", ".avi", ".mkv", ".webm")
            ),
        }

    def recall(
        self,
        *,
        query: str = "",
        top_k: int | None = None,
        platform: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """按策略召回。

        指定 ``platform`` 时按该平台报告里要求的**画面位次**逐格挑图：
        每个位次一个候选池（顶层品类 + 关键词筛选），再走统一的
        冷却过滤 + 新图加权 + 加权随机采样。某个位次没有合适素材时
        返回空 picks，页面会提示"这一格需要补拍"。

        不指定平台时退回原来的关键词召回（控制台用关键词相似度替代向量检索）。
        """
        assets = self.assets()
        extra_terms = tuple(term for term in query.replace(",", " ").split() if term)
        profile = platform_profile(platform)
        if profile is None:
            candidates = [
                MediaCandidate(
                    asset=asset, similarity=RecallPolicy.keyword_similarity(query, asset)
                )
                for asset in assets
            ]
            picks = self.policy.recall(candidates, top_k=top_k, now=now)
            return {
                "query": query,
                "platform": None,
                "slots": [],
                "count": len(picks),
                "picks": [self._pick_payload(pick) for pick in picks],
            }

        limit = top_k or 1
        # 先按档位分批、再轮询各个位次：这样"精准档"对所有位次都先跑一遍，
        # 不会出现靠前的位次用放宽档把靠后位次唯一匹配的那张图抢走。
        pools_by_order = {
            slot.order: slot_pools(slot, assets, extra_terms=extra_terms)
            for slot in profile.slots
        }
        picks_by_order: dict[int, list[Any]] = {slot.order: [] for slot in profile.slots}
        levels_by_order: dict[int, list[str]] = {slot.order: [] for slot in profile.slots}
        used_ids: set[str] = set()

        level_names = [level for level, _pool in pools_by_order[profile.slots[0].order]]
        for level in level_names:
            for slot in profile.slots:
                picked = picks_by_order[slot.order]
                if len(picked) >= limit:
                    continue
                pool = next(
                    (items for key, items in pools_by_order[slot.order] if key == level), []
                )
                fresh = [
                    (score, asset) for score, asset in pool if asset.asset_id not in used_ids
                ]
                if not fresh:
                    continue
                got = self.policy.recall(
                    [MediaCandidate(asset=asset, similarity=score) for score, asset in fresh],
                    top_k=limit - len(picked),
                    now=now,
                )
                if got:
                    picked.extend(got)
                    levels_by_order[slot.order].append(level)
                    used_ids.update(pick.asset_id for pick in got)

        slots: list[dict[str, Any]] = []
        flat: list[dict[str, Any]] = []
        for slot in profile.slots:
            picks = picks_by_order[slot.order]
            levels = levels_by_order[slot.order]
            pools = pools_by_order[slot.order]
            payloads = [self._pick_payload(pick) for pick in picks]
            weakest = weakest_level(levels)
            strict_size = len(pools[0][1]) if pools else 0
            widest = max((len(pool) for _level, pool in pools), default=0)
            slots.append(
                {
                    "order": slot.order,
                    "role": slot.role,
                    "note": slot.note,
                    "media_kind": slot.media_kind,
                    # 严格池有多少候选（原来的 matched 口径），便于判断"是素材真的没有"
                    "matched": strict_size,
                    "available": widest,
                    "level": weakest,
                    "level_label": level_label(weakest),
                    "relaxed": weakest in RELAXED_LEVELS,
                    "picks": payloads,
                    # 一张都没出但候选池不空：说明是全在冷却期（或已被同帖别的位次占用）
                    "blocked_by_cooldown": widest > 0 and not picks,
                }
            )
            flat.extend(payloads)
        return {
            "query": query,
            "platform": profile.as_payload(),
            "slots": slots,
            "count": len(flat),
            "requested": limit * len(profile.slots),
            "shortfall": max(0, limit * len(profile.slots) - len(flat)),
            "picks": flat,
        }

    # ---------- 预览与短文 ----------
    def find_duplicate(self, *, content_hash: str, size: int | None = None) -> dict[str, Any] | None:
        """查这份内容是否已经在库里（登记表 + 既有素材目录）。

        **先查重再识别**：重复文件本来会在入库时才被拒，但那时识别费已经花掉了。
        为了不每次都把 1.3 GB 素材库重算一遍哈希，先用**字节数**过滤——
        stat 几百个文件只要几毫秒，只有尺寸相同的极少数文件才需要真算哈希，
        因此这是"精确且几乎零成本"的查重。
        """
        digest = (content_hash or "").strip().lower()
        if not digest:
            return None
        entry = self.registry.find_by_hash(digest)
        if entry:
            return {
                "where": "registry",
                "asset_id": str(entry.get("asset_id") or ""),
                "file_name": str(entry.get("file_name") or ""),
                "category": "/".join(
                    part
                    for part in (str(entry.get("process") or ""), str(entry.get("sub_process") or ""))
                    if part
                ),
                "path": str(entry.get("source_path") or ""),
            }
        root = self.media_root
        if not root.is_dir():
            return None
        for path in _iter_media_files(root):
            try:
                if size is not None and path.stat().st_size != size:
                    continue
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    continue
            except OSError:
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                relative = path
            parts = relative.parts
            return {
                "where": "library",
                "asset_id": "",
                "file_name": path.name,
                "category": "/".join(parts[:-1]),
                "path": str(path),
            }
        return None

    def precheck(self, *, content_hash: str, size: int | None = None) -> dict[str, Any]:
        """给页面用的"上传前查重"：命中就不必上传、更不必识别。"""
        found = self.find_duplicate(content_hash=content_hash, size=size)
        if found is None:
            return {"ok": True, "duplicate": False}
        return {
            "ok": True,
            "duplicate": True,
            "existing": found,
            "message": _duplicate_message(found),
        }

    def asset_by_id(self, asset_id: str) -> MediaAsset | None:
        target = (asset_id or "").strip()
        if not target:
            return None
        for asset in self.assets():
            if asset.asset_id == target:
                return asset
        return None

    def vision_check(self) -> dict[str, Any]:
        """视觉模型自检：把"到底哪一步不行"讲清楚，不用去翻 .env 猜。"""
        from pulse.services.media.describe import probe_vision

        config = self.vision_config
        result = probe_vision(config, client=self.caption_client)
        return {
            "ok": bool(result.get("ok")),
            "stage": result.get("stage", ""),
            "message": result.get("message", ""),
            "hint": result.get("hint", ""),
            "endpoint": config.endpoint,
            "model": config.model,
            "session": config.session,
            "timeout_s": config.timeout_s,
            "key_configured": config.enabled,
            # 自检只证明"能识图"，不消耗素材台账
            "detail": {
                "base_url": config.base_url,
                "api_style": config.api_style,
                "max_output_tokens": config.max_output_tokens,
                "caption_max_tokens": config.caption_max_tokens,
                "export_root": str(self.export_root),
            },
        }

    def media_path(self, asset: MediaAsset) -> Path | None:
        """定位素材文件。

        描述头部里的 ``source_path`` 写的是**别的机器上的绝对路径**（`D:/菲美得/…`），
        不能直接用。可靠做法是从描述文件的相对路径推出素材目录，再按文件名找。
        """
        try:
            root = self.media_root.resolve()
            relative = Path(asset.description_path).resolve().relative_to(self.rag_root.resolve())
        except (ValueError, OSError):
            return None
        folder = root / relative.parent
        name = Path(str(asset.file_name).replace("\\", "/")).name  # 防路径穿越
        if not name:
            return None
        candidate = folder / name
        if candidate.is_file() and _is_within(candidate, root):
            return candidate
        # 描述里的文件名可能与实际文件不同（例如少了扩展名）
        for path in sorted(folder.glob(f"{glob.escape(Path(name).stem)}.*")):
            if path.is_file() and _is_within(path, root):
                return path
        return None

    def caption(
        self,
        *,
        platform: str,
        asset_ids: tuple[str, ...] | list[str] = (),
        extra_note: str = "",
    ) -> dict[str, Any]:
        """按平台模式生成短文。

        不指定素材时，用该平台当前召回出来的图（每个位次一张）。
        """
        spec = caption_spec(platform)
        if spec is None:
            raise ValueError(
                f"不支持的平台：{platform!r}（可选：{', '.join(CAPTION_SPECS)}）"
            )
        catalog = {asset.asset_id: asset for asset in self.assets()}
        picked = [catalog[item] for item in asset_ids if item in catalog]
        if not picked:
            recalled = self.recall(platform=platform, top_k=1)
            picked = [
                catalog[item["asset_id"]]
                for item in recalled["picks"]
                if item["asset_id"] in catalog
            ]
        if not picked:
            raise ValueError("这个平台当前没有可用素材，先召回或补拍后再生成短文。")

        result = generate_caption(
            self.vision_config,
            platform=platform,
            materials=picked,
            client=self.caption_client,
            extra_note=extra_note,
        )
        return {
            "ok": True,
            "platform_name": spec.name,
            "spec": spec.as_payload(),
            "material_count": len(picked),
            **result.as_payload(),
        }

    def export(
        self,
        *,
        platform: str,
        slots: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        caption: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把召回结果导出成素材包（原图按位次顺序 + 说明 + 文案）。

        由页面把**当前显示出来的**位次与素材原样回传，保证"所见即所得"——
        导出时重跑一次召回会因加权随机而挑出别的图。
        """
        profile = platform_profile(platform)
        spec = caption_spec(platform)
        if profile is None or spec is None:
            raise ValueError(f"不支持的平台：{platform!r}")

        catalog = {asset.asset_id: asset for asset in self.assets()}
        roles = {slot.order: slot for slot in profile.slots}
        entries: list[ExportEntry] = []
        for index, item in enumerate(slots, start=1):
            order = int(item.get("order") or index)
            slot = roles.get(order)
            role = str(item.get("role") or (slot.role if slot else ""))
            asset = catalog.get(str(item.get("asset_id") or ""))
            entries.append(
                ExportEntry(
                    order=order,
                    role=role,
                    file_name=asset.file_name if asset else str(item.get("file_name") or "未知"),
                    category=asset.category if asset else "",
                    summary=asset.summary if asset else "",
                    note=slot.note if slot else "",
                    source=self.media_path(asset) if asset else None,
                    asset_id=asset.asset_id if asset else None,
                )
            )
        if not entries:
            raise ValueError("没有可导出的素材，请先执行召回。")

        summary = (
            f"平台：{profile.name}（{profile.priority}）"
            f"｜画幅 {profile.aspect}｜{profile.shot_count}｜{profile.form}"
            f"｜文案结构：{spec.structure_text}"
        )
        result = export_entries(
            root=self.export_root,
            label=profile.name,
            entries=entries,
            caption=caption,
            spec_summary=summary,
        )
        payload = result.as_payload()
        payload["label"] = profile.name
        # 导出 = 这一帖的素材已定稿、交给人工发布，按契约 §2.1 属于"实际进入发布"，
        # 因此在这里写台账。content_id 用导出目录名：同一次导出重复调用不会重复消耗冷却。
        payload["used"] = self.mark_exported_used(
            result.used_asset_ids, content_id=result.directory.name
        )
        payload["cooldown_days"] = self.config.cooldown_days
        return {"ok": True, **payload}


#: 查重时遍历的素材后缀（与入库白名单一致）
_MEDIA_SUFFIXES = frozenset(
    {
        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".psd", ".psb",
        ".mp4", ".mov", ".avi", ".mkv", ".webm",
    }
)


def _iter_media_files(root: Path):
    """遍历素材目录里的媒体文件（跳过隐藏目录与导出目录）。"""
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _MEDIA_SUFFIXES:
            continue
        if any(part.startswith((".", "_")) for part in path.relative_to(root).parts[:-1]):
            continue
        yield path


def _duplicate_message(found: dict[str, Any]) -> str:
    """把"这份内容已经在库里"说清楚，并说明为什么没花钱去识别。"""
    name = found.get("file_name") or "（未知文件）"
    category = found.get("category") or "（未知品类）"
    where = "已入库的素材" if found.get("where") == "registry" else "既有素材库"
    return (
        f"这份内容已经存在于{where}：{name}（{category}）。"
        "已跳过识别，没有产生任何模型费用。"
    )


def _content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
        # PSD 预览时已经转成 PNG（见 _preview_payload），这里只是兜底
        ".psd": "image/png",
        ".psb": "image/png",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
    }.get(suffix, "application/octet-stream")


def _preview_payload(path: Path) -> tuple[bytes, str]:
    """预览用字节流。

    浏览器不认识 Photoshop 文档，所以 ``.psd`` / ``.psb`` 先解出合并图再转 PNG；
    其它格式原样回传（零修饰，字节级）。转换只做解码 + 必要的降采样。
    """
    data = path.read_bytes()
    if path.suffix.lower() in PSD_SUFFIXES:
        return psd_to_png(data, max_side=PSD_PREVIEW_MAX_SIDE), "image/png"
    return data, _content_type_for(path)


def _disposition_value(disposition: str, key: str) -> str:
    marker = f'{key}="'
    start = disposition.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = disposition.find('"', start)
    return disposition[start:end] if end > start else ""


def parse_multipart(
    body: bytes, boundary: str
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """解析 multipart/form-data（标准库实现，不依赖 cgi 与三方库）。"""
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    delimiter = ("--" + boundary).encode("utf-8")
    for chunk in body.split(delimiter):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        header_blob, separator, raw = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        headers: dict[str, str] = {}
        for line in header_blob.decode("utf-8", "replace").splitlines():
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        name = _disposition_value(headers.get("content-disposition", ""), "name")
        if not name:
            continue
        file_name = _disposition_value(headers.get("content-disposition", ""), "filename")
        if file_name:
            files[name] = (file_name, raw)
        else:
            fields[name] = raw.decode("utf-8", "replace")
    return fields, files


class _Handler(BaseHTTPRequestHandler):
    app: MediaConsoleApp
    server_version = "PulseMediaConsole/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 标准库签名
        """静默访问日志。"""

    def do_GET(self) -> None:  # noqa: N802 - 标准库命名
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_bytes(PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send_json(self.app.state())
        elif path == "/api/asset-image":
            self._handle_asset_image(parse_qs(parsed.query))
        else:
            self._send_json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - 标准库命名
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            if path == "/api/assets":
                self._handle_upload(body)
            elif path == "/api/describe":
                self._handle_describe(body)
            elif path == "/api/usage":
                payload = json.loads(body or b"{}")
                self._send_json(
                    self.app.mark_used(
                        asset_id=str(payload.get("asset_id", "")),
                        content_id=payload.get("content_id"),
                    )
                )
            elif path == "/api/recall":
                payload = json.loads(body or b"{}")
                top_k = payload.get("top_k")
                self._send_json(
                    self.app.recall(
                        query=str(payload.get("query", "")),
                        top_k=int(top_k) if top_k else None,
                        platform=str(payload.get("platform", "")),
                    )
                )
            elif path == "/api/caption":
                payload = json.loads(body or b"{}")
                raw_ids = payload.get("asset_ids") or []
                self._send_json(
                    self.app.caption(
                        platform=str(payload.get("platform", "")),
                        asset_ids=[str(item) for item in raw_ids],
                        extra_note=str(payload.get("extra_note", "")),
                    )
                )
            elif path == "/api/export":
                payload = json.loads(body or b"{}")
                raw_slots = payload.get("slots") or []
                raw_caption = payload.get("caption")
                self._send_json(
                    self.app.export(
                        platform=str(payload.get("platform", "")),
                        slots=[
                            item for item in raw_slots if isinstance(item, dict)
                        ],
                        caption=raw_caption if isinstance(raw_caption, dict) else None,
                    )
                )
            elif path == "/api/vision-check":
                self._send_json(self.app.vision_check())
            elif path == "/api/precheck":
                payload = json.loads(body or b"{}")
                size = payload.get("size")
                self._send_json(
                    self.app.precheck(
                        content_hash=str(payload.get("hash", "")),
                        size=int(size) if size else None,
                    )
                )
            else:
                self._send_json({"ok": False, "error": "not found"}, status=404)
        except (UnsupportedMediaError, DuplicateMediaError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
        except VisionRequiredError as exc:
            self._send_json(
                {"ok": False, "error": str(exc), "vision_required": True}, status=403
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"ok": False, "error": f"请求不合法：{exc}"}, status=400)

    def _parse_upload(self, body: bytes) -> tuple[dict[str, str], str, bytes]:
        """解析 multipart 上传，返回 (表单字段, 文件名, 文件内容)。"""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type or "boundary=" not in content_type:
            raise ValueError("需要 multipart/form-data 上传")
        boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
        fields, files = parse_multipart(body, boundary)
        if "file" not in files:
            raise ValueError("缺少 file 字段")
        file_name, data = files["file"]
        return fields, file_name, data

    def _handle_upload(self, body: bytes) -> None:
        """入库：表单里带描述就用表单的，空着则由应用层自动生成。"""
        fields, file_name, data = self._parse_upload(body)
        keywords = tuple(
            item.strip() for item in (fields.get("keywords") or "").split(",") if item.strip()
        )
        self._send_json(
            self.app.upload(
                file_name=file_name,
                data=data,
                process=fields.get("process", "").strip(),
                sub_process=fields.get("sub_process", "").strip(),
                keywords=keywords,
                summary=fields.get("summary", "").strip(),
                details=fields.get("details", "").strip(),
                vision_ticket=fields.get("vision_ticket", "").strip(),
            )
        )

    def _handle_describe(self, body: bytes) -> None:
        """选图后自动生成描述（不入库）。"""
        fields, file_name, data = self._parse_upload(body)
        self._send_json(
            self.app.describe(
                file_name=file_name,
                data=data,
                # 页面传来的就是用户在下拉框里选的东西，**原样透传**：
                # 空的子类是有意义的（「人员」这类品类本来就没有子类），
                # 一旦改写成"未分类"，用户手选的品类就对不上库里的品类清单，
                # 会被判成"没能判断出品类"而丢掉。
                process=fields.get("process", "").strip(),
                sub_process=fields.get("sub_process", "").strip(),
            )
        )

    def _handle_asset_image(self, query: dict[str, list[str]]) -> None:
        """素材预览：按 asset_id 回传文件本体（图片直接看，视频可播放）。"""
        asset_id = (query.get("asset_id") or [""])[0]
        asset = self.app.asset_by_id(asset_id)
        if asset is None:
            self._send_json({"ok": False, "error": "素材不存在"}, status=404)
            return
        path = self.app.media_path(asset)
        if path is None:
            self._send_json({"ok": False, "error": "素材文件已不在磁盘上"}, status=404)
            return
        try:
            data, content_type = _preview_payload(path)
        except UnsupportedPsdError as exc:
            self._send_json({"ok": False, "error": f"PSD 无法预览：{exc}"}, status=500)
            return
        except OSError as exc:
            self._send_json({"ok": False, "error": f"读取失败：{exc}"}, status=500)
            return
        self._send_file(data, content_type)

    def _send_file(self, data: bytes, content_type: str) -> None:
        """发送文件内容；支持 Range，否则视频无法拖动进度。"""
        total = len(data)
        start, end, partial = 0, total - 1, False
        match = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else total - 1
            else:
                start = max(0, total - int(match.group(2)))
            end = min(end, total - 1)
            if start > end or start >= total:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
            partial = True
        chunk = data[start : end + 1]
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=600")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        self.wfile.write(chunk)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        self._send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def _send_bytes(self, data: bytes, content_type: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def create_server(
    app: MediaConsoleApp, *, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    """创建控制台 HTTP 服务（port=0 时由系统分配空闲端口）。"""
    handler = type("BoundHandler", (_Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:  # pragma: no cover - 手工启动入口
    import argparse

    parser = argparse.ArgumentParser(description="Pulse 素材库控制台")
    parser.add_argument("--rag-root", default="RAG知识库/图片描述")
    parser.add_argument("--media-root", default="../菲美得产品图片")
    parser.add_argument("--ledger", default=".pulse/media_ledger.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = MediaConsoleApp(
        rag_root=args.rag_root,
        media_root=args.media_root,
        ledger_path=args.ledger,
        config=RecallConfig(brand=BRAND_NAME),
    )
    server = create_server(app, host=args.host, port=args.port)
    print(f"素材库控制台已启动：http://{args.host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        server.shutdown()


if __name__ == "__main__":  # pragma: no cover
    main()
