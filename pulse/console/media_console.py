"""素材库图形化控制台（所有者：root）。

纯标准库实现（http.server + 内嵌 HTML），不引入任何新依赖：

- ``GET  /``             素材库页面（上传、容量预警、冷却状态）
- ``GET  /api/state``    素材清单 + 容量报告
- ``POST /api/assets``   上传实拍图入库（multipart/form-data，需视觉识别凭据）
- ``POST /api/describe`` 选图后自动生成描述（摘要 / 细节 / 关键词），
  识别成功时同时签发"入库许可"凭据
- ``POST /api/usage``    标记素材已进入内容 / 发布（触发 15 天冷却）
- ``POST /api/recall``   按策略召回候选（新图优先 + 冷却过滤 + 加权随机）

**入库许可（2026-09-11 新增）**：只有完成视觉识别的图片才允许入库。控制台上按钮在
识别成功前是灰色不可点的；服务端同样校验凭据，绕过页面直接调接口也进不来。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
from pulse.services.media.ingest import (
    DuplicateMediaError,
    MediaIngestor,
    UnsupportedMediaError,
)
from pulse.services.media.ledger import RecallLedger
from pulse.services.media.policy import MediaCandidate, RecallPolicy
from pulse.services.media.platforms import (
    platform_choices,
    platform_profile,
    prefer_media_kind,
    slot_score,
    top_tier,
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
    "button[disabled]{background:#c9ced6;color:#f2f3f5;border:1px solid #c9ced6;"
    "cursor:not-allowed}"
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
    "第一步：在「上传实拍图入库」里选好品类，把电脑里的产品实拍图选进来；页面会自动识别画面内容。<br/>"
    "识别成功前，「上传并入库」按钮是<span class='muted'>灰色、点不动</span>的；"
    "识别成功后会变成蓝色，这时才能点。<br/>"
    "第二步：看最上方的横幅——绿色表示素材充足；红色表示可用图片少于 112 张（约一周用量），需要尽快补拍。<br/>"
    "第三步：某张图被内容用掉后，点它卡片上的“标记已用于内容”，这张图 15 天内不会再被选中。"
    "</p></section>"
    "<section><h2>上传实拍图入库</h2>"
    "<p class='muted'>入库许可：图片必须先通过视觉识别。识别没成功，按钮保持灰色，无法入库。</p>"
    "<form id='upload'>"
    "<div class='row'>"
    "<div class='field'><label>图片文件</label>"
    "<input type='file' name='file' accept='image/*' required/></div>"
    "<div class='field'><label>品类 process</label>"
    "<select name='process' id='process-select' required></select></div>"
    "<div class='field'><label>子类 sub_process</label>"
    "<select name='sub_process' id='sub-process-select' required></select></div>"
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
    "<input name='query' placeholder='例如 阀体 配重'/></div>"
    "<div class='field'><label>操作</label>"
    "<button type='submit'>执行召回</button></div>"
    "</div></form>"
    "<div class='hint' id='platform-summary'></div>"
    "<div id='recall-result'></div></section>"
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
    "function describeFile(){var f=document.getElementById('upload');"
    "var file=f.querySelector('input[name=file]').files[0];"
    "document.getElementById('vision-ticket').value='';"
    "document.getElementById('upload-msg').textContent='';"
    "if(!file){document.getElementById('describe-box').style.display='none';"
    "setGate(false,'请先选择图片，识别成功后按钮才会变亮。',false);return;}"
    "setGate(false,'正在识别图片…识别完成前不能入库。',false);"
    "var fd=new FormData();fd.append('file',file);"
    "fd.append('process',chosenProcess());"
    "fd.append('sub_process',chosenSub());"
    "document.getElementById('describe-status').textContent='正在识别图片并生成描述，请稍等…';"
    "fetch('/api/describe',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){"
    "if(!d.ok){document.getElementById('describe-status').textContent='自动生成失败：'+d.error;"
    "setGate(false,'视觉识别失败，无法入库。',true);return;}"
    "document.getElementById('describe-box').style.display='block';"
    "f.querySelector('input[name=summary]').value=d.summary||'';"
    "f.querySelector('input[name=keywords]').value=(d.keywords||[]).join(', ');"
    "f.querySelector('textarea[name=details]').value=d.details||'';"
    "var msg=d.source==='vision'?'已用视觉模型自动生成描述':"
    "'已按文件名与规格信息自动生成（未配置视觉模型，画面内容待补充）';"
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
    "('视觉识别未完成，不能入库。请检查 .env 里的视觉模型配置，然后重新选图。'),"
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
    "var body=s.picks.length?s.picks.map(function(p){return photoHtml(p);}).join('')"
    ":(s.blocked_by_cooldown"
    "?\"<p class='muted'>这一格有符合的素材，但都在 15 天冷却期内，暂时不能用。"
    "可以先换别的图，或等冷却结束。</p>\""
    ":\"<p class='muted'>这一格还没有合适的素材，建议按这个位次补拍。</p>\");"
    "return \"<div class='slot'><h3>\"+esc(s.role)+\"</h3>\""
    "+(s.note?(\"<p class='muted'>\"+esc(s.note)+\"</p>\"):'')+body+'</div>';}).join('');});});"
    "function photoHtml(p){return \"<div class='asset'><h3>\"+esc(p.file_name)+\"</h3>\""
    "+\"<span class='tag\"+(p.is_new?' new':'')+\"'>\"+esc(p.category)+\"</span>\""
    "+\"<p class='muted'>权重 \"+p.weight+\" ｜ 相似度 \"+p.similarity+\"</p>\""
    "+\"<p>\"+esc(p.summary)+\"</p></div>\";}"
    "refresh();</script></body></html>"
)


class VisionRequiredError(ValueError):
    """入库许可未满足：这张图还没有完成视觉识别。"""


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
        self.describer = describer or build_describer(root, catalog=self._catalog)
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
        asset = self.ingestor.add_image(
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

        只有视觉识别成功（``source == "vision"``）才签发入库许可凭据。
        """
        try:
            description = self.describer(
                data, file_name=file_name, process=process, sub_process=sub_process
            )
        except Exception as exc:  # 网络 / 额度 / 解析失败都不应阻断入库
            description = replace(
                heuristic_describe(
                    data, file_name=file_name, process=process, sub_process=sub_process
                ),
                warnings=(
                    f"视觉模型调用失败，已退回基础信息描述：{exc}",
                    "画面内容待人工补充。",
                ),
            )
        vision_ok = description.source == "vision"
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
        slots: list[dict[str, Any]] = []
        flat: list[dict[str, Any]] = []
        for slot in profile.slots:
            scored = [
                (slot_score(slot, asset, extra_terms=extra_terms), asset) for asset in assets
            ]
            scored = [(score, asset) for score, asset in scored if score > 0]
            # 报告指定的"首选形态"（视频 / 图集）是硬口径，不能靠权重随机压过去
            scored = prefer_media_kind(slot, scored)
            # 位次要求明确：只在最高分那一档里随机，保证挑得准
            scored = top_tier(scored)
            candidates = [
                MediaCandidate(asset=asset, similarity=score) for score, asset in scored
            ]
            picks = self.policy.recall(candidates, top_k=limit, now=now)
            payloads = [self._pick_payload(pick) for pick in picks]
            slots.append(
                {
                    "order": slot.order,
                    "role": slot.role,
                    "note": slot.note,
                    "media_kind": slot.media_kind,
                    "matched": len(scored),
                    # 有素材但全在冷却期：提示"等冷却"，而不是让人白跑一趟去补拍
                   "picks": payloads,
                    "blocked_by_cooldown": bool(scored) and not picks,
                }
            )
            flat.extend(payloads)
        return {
            "query": query,
            "platform": profile.as_payload(),
            "slots": slots,
            "count": len(flat),
            "picks": flat,
        }


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
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_bytes(PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send_json(self.app.state())
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
                process=fields.get("process", "").strip() or "未分类",
                sub_process=fields.get("sub_process", "").strip() or "未分类",
            )
        )

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
