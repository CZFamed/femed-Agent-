"""内容流水线控制台（审批台）——观众：内容运营与市场负责人。

它把契约 §7 的 11 个端点**直接暴露在控制台同一进程的 `/api/v1/*` 下**（由
`pulse.api.http_server.ApiRouteMixin` 转发），所以页面用的是真实 HTTP 调用，
而不是"页面里再写一套业务逻辑"。运营在一个浏览器页面上就能走完：

    brief → 草稿 → 提交待审 → 通过/驳回/改稿 → 排期 → 立即发布 → 半自动导出

两处刻意的设计：

1. **合规与强制复核在页面上可见**：每条草稿显示 block/warn 与"必须逐条复核"的三个触发条件，
   因为 FR-4 的验收标准就是"一键通过/修改/整批 + 留痕"，看不见就等于没实现。
2. **KPI 用询盘口径**：看板区不使用点赞数（B2B 决策周期 3–18 个月）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pulse.services.compliance import VariantView


@dataclass
class PipelineConsole:
    """审批台的后端：聚合状态 + 页面。业务动作一律通过 `ApiApp` 走一遍。"""

    app: Any
    #: 演示接线提示（起服务时打一行日志，页面上也会标出来）
    demo: bool = True

    # ---------- 聚合状态 ----------

    def state(self) -> dict[str, Any]:
        """一次请求把页面需要的所有数据取齐（避免前端连打十几个请求）。"""
        accounts = self._safe_body("GET", "/api/v1/accounts").get("accounts", [])
        return {
            "demo": self.demo,
            "version": _version(),
            "dashboard": self.app.dashboard(),
            "accounts": accounts,
            "variants": self._variants(),
            "schedules": self._schedules(),
        }

    def _variants(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in self.app.content.store.list_variants():
            payload = self.app.variant_payload(record)
            payload["compliance"] = self._compliance_summary(record)
            rows.append(payload)
        # 待审的排前面，便于运营一进来就看到要处理的
        order = {"pending_review": 0, "draft": 1, "needs_revision": 2, "approved": 3}
        rows.sort(key=lambda item: (order.get(str(item.get("status")), 9), str(item.get("id"))))
        return rows

    def _compliance_summary(self, record: Any) -> dict[str, Any]:
        compliance = getattr(self.app, "compliance", None)
        if compliance is None:
            return {"available": False}
        outcome = compliance.outcome_for(record.id)
        view = VariantView.from_variant_record(record, platform=record.platform.value)
        triggers = compliance.must_review_individually(view, outcome)
        return {
            "available": True,
            "blocked": outcome.blocked,
            "blocking": [{"rule": item.rule, "message": item.message} for item in outcome.blocking],
            "warnings": [{"rule": item.rule, "message": item.message} for item in outcome.warnings],
            "must_review_individually": list(triggers),
        }

    def _schedules(self) -> list[dict[str, Any]]:
        dispatcher = getattr(self.app, "dispatcher", None)
        if dispatcher is None:
            return []
        rows: list[dict[str, Any]] = []
        for schedule in dispatcher.schedules.list_schedules():
            job = dispatcher.jobs.get_by_schedule(schedule.id)
            rows.append(
                {
                    "id": schedule.id,
                    "variant_id": schedule.variant_id,
                    "account_id": schedule.account_id,
                    "timezone": schedule.timezone,
                    "scheduled_at": schedule.scheduled_at.isoformat(),
                    "status": schedule.status_value.value,
                    "job": None
                    if job is None
                    else {
                        "id": job.id,
                        "status": job.status_value.value,
                        "attempts": job.attempts,
                        "unified_post_id": job.unified_post_id,
                    },
                }
            )
        rows.sort(key=lambda item: item["scheduled_at"], reverse=True)
        return rows

    def _safe_body(self, method: str, path: str) -> Mapping[str, Any]:
        response = self.app.handle(method, path)
        return response.body if isinstance(response.body, Mapping) else {}

    # ---------- 页面 ----------

    def page(self) -> str:
        return PIPELINE_PAGE_HTML.replace("__DEMO_BANNER__", _banner(self.demo))


def _banner(demo: bool) -> str:
    if not demo:
        return ""
    return (
        '<div class="warn">当前是<b>演示接线</b>：内存存储 + 假队列 + 占位账号，'
        "不会真的发到平台；真实接线请在部署层调用 <code>build_service_app()</code>。</div>"
    )


def _version() -> str:
    from pulse import __version__

    return __version__


PIPELINE_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pulse 内容流水线（审批台）</title>
<style>
:root{--ink:#0E2A47;--accent:#FF6B2C;--bg:#F4F7FB;--line:#D9DDE0;--muted:#62748A;--ok:#1F8A4C;--warn:#B26A00;--bad:#C0392B}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;color:#1F2D3D;background:var(--bg)}
header{background:var(--ink);color:#fff;padding:14px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header h1{font-size:16px;margin:0;font-weight:600}
header .sp{flex:1}
header a{color:#cfe0f5;text-decoration:none;border:1px solid #3d5a7a;padding:4px 10px;border-radius:6px}
.wrap{max-width:1180px;margin:0 auto;padding:20px}
.warn{background:#FFF6E5;border:1px solid #F0D08A;color:#7A5200;padding:10px 12px;border-radius:8px;margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:22px;color:var(--ink)}
.card span{color:var(--muted);font-size:12px}
section{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:18px}
section h2{font-size:15px;margin:0 0 12px;color:var(--ink)}
label{display:block;font-size:12px;color:var(--muted);margin:8px 0 4px}
input,select,textarea{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;font:inherit;background:#fff}
.row{display:flex;gap:10px;flex-wrap:wrap}
.row>div{flex:1;min-width:180px}
button{cursor:pointer;border:1px solid var(--line);background:#fff;border-radius:6px;padding:6px 11px;font:inherit}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.ghost{background:#fff}
button:disabled{opacity:.5;cursor:not-allowed}
.item{border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:10px}
.item h3{margin:0 0 6px;font-size:14px}
.tag{display:inline-block;font-size:12px;padding:1px 8px;border-radius:999px;border:1px solid var(--line);margin-right:6px;background:#F7F9FC}
.tag.ok{color:var(--ok);border-color:#BFE3CC;background:#F1FBF5}
.tag.warn{color:var(--warn);border-color:#F0D08A;background:#FFF9EC}
.tag.bad{color:var(--bad);border-color:#F1C2BC;background:#FDF3F2}
.muted{color:var(--muted);font-size:12px}
.findings{margin:8px 0 0;padding-left:18px;color:var(--bad);font-size:12px}
.findings li{margin:2px 0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
dialog{border:none;border-radius:10px;padding:0;max-width:720px;width:92%}
dialog::backdrop{background:rgba(14,42,71,.45)}
.dlg-head{background:var(--ink);color:#fff;padding:10px 14px;display:flex;align-items:center}
.dlg-head b{flex:1;font-size:14px}
.dlg-body{padding:14px;max-height:60vh;overflow:auto}
pre{white-space:pre-wrap;word-break:break-word;background:#F7F9FC;border:1px solid var(--line);border-radius:6px;padding:10px;font:12px/1.6 Consolas,monospace}
code{background:#F0F4F9;padding:1px 4px;border-radius:4px}
</style>
</head>
<body>
<header>
  <h1>Pulse 内容流水线 · 审批台</h1>
  <span class="muted" id="ver"></span>
  <span class="sp"></span>
  <a href="/">返回素材库</a>
</header>
<div class="wrap">
__DEMO_BANNER__

<div class="cards" id="cards"></div>

<section>
  <h2>1. 提交 brief，生成各平台草稿</h2>
  <div class="row">
    <div><label>主题 topic</label><input id="f-topic" placeholder="例：消失模铸造 + 粗加工一体化产能"></div>
    <div><label>目标读者 target_audience</label><input id="f-audience" placeholder="例：印度机床整机厂采购与供应链负责人"></div>
  </div>
  <div class="row">
    <div><label>平台 platforms</label><input id="f-platforms" value="linkedin" placeholder="逗号分隔，如 linkedin,youtube"></div>
    <div><label>期望行动 CTA（可空）</label><input id="f-cta" placeholder="例：Send us your drawing"></div>
  </div>
  <div class="actions">
    <button class="primary" onclick="submitBrief()">生成草稿</button>
    <span class="muted" id="brief-hint">生成后到下面走审批；未 approved 的草稿不允许排期。</span>
  </div>
</section>

<section>
  <h2>2. 草稿与审批（通过 / 驳回 / 改稿，留痕含备注）</h2>
  <div id="variants" class="muted">加载中…</div>
</section>

<section>
  <h2>3. 排期与发布（立即发布 / 重排 / 取消 / 半自动导出）</h2>
  <div id="schedules" class="muted">加载中…</div>
</section>

<section>
  <h2>4. 制裁与出口管制筛查（留痕，非法律结论）</h2>
  <div class="row">
    <div><label>主体名（客户 / 公司）</label><input id="f-subject" placeholder="例：Haas Automation"></div>
    <div><label>执行人</label><input id="f-checker" value="ops"></div>
  </div>
  <div class="actions"><button onclick="screenSubject()">执行筛查</button></div>
  <div id="screen-result" class="muted"></div>
</section>

<section>
  <h2>5. 看板（询盘与触达口径，不使用点赞数）</h2>
  <div id="dashboard" class="muted">加载中…</div>
</section>
</div>

<dialog id="dlg">
  <div class="dlg-head"><b id="dlg-title">半自动导出</b><button class="ghost" onclick="document.getElementById('dlg').close()">关闭</button></div>
  <div class="dlg-body" id="dlg-body"></div>
</dialog>

<script>
const TZ = [["+05:30","印度 IST"],["+08:00","中国 CST"],["-07:00","美西 PDT"],["+03:00","土耳其 TRT"],["+00:00","UTC"]];
let STATE = {variants:[],schedules:[]};

async function api(method, path, body){
  const opt = {method, headers:{}};
  if(body !== undefined){ opt.headers["Content-Type"]="application/json"; opt.body=JSON.stringify(body); }
  const res = await fetch(path, opt);
  let data = {};
  try { data = await res.json(); } catch(e){ data = {}; }
  if(!res.ok){
    const msg = (data.error && data.error.message) || ("HTTP " + res.status);
    throw new Error(msg);
  }
  return data;
}

function tag(text, cls){ return '<span class="tag '+(cls||"")+'">'+esc(text)+'</span>'; }
function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

async function refresh(){
  const s = await api("GET","/api/pipeline/state");
  STATE = s;
  document.getElementById("ver").textContent = "v" + s.version;
  renderCards(s.dashboard, s);
  renderVariants(s.variants);
  renderSchedules(s.schedules, s.variants);
  renderDashboard(s.dashboard);
}

function renderCards(d, s){
  const by = (d.content && d.content.by_status) || {};
  const p = d.publishing || {};
  document.getElementById("cards").innerHTML = [
    ["待审", by.pending_review||0],["草稿", by.draft||0],["已通过", by.approved||0],
    ["已驳回/改稿", (by.rejected||0)+(by.needs_revision||0)],
    ["排期", p.schedules_total||0],["发布成功", p.published||0]
  ].map(([k,v])=>'<div class="card"><b>'+v+'</b><span>'+k+'</span></div>').join("");
}

async function submitBrief(){
  const topic = document.getElementById("f-topic").value.trim();
  const audience = document.getElementById("f-audience").value.trim();
  const platforms = document.getElementById("f-platforms").value.split(",").map(x=>x.trim()).filter(Boolean);
  const cta = document.getElementById("f-cta").value.trim();
  if(!topic || !audience || !platforms.length){ alert("主题、目标读者、平台都要填"); return; }
  const body = {topic, target_audience:audience, platforms};
  if(cta) body.cta = cta;
  document.getElementById("brief-hint").textContent = "生成中…";
  try{
    const r = await api("POST","/api/v1/briefs",body);
    document.getElementById("brief-hint").textContent = "已生成 "+r.variants.length+" 条草稿，去下面提交待审";
    await refresh();
  }catch(e){ document.getElementById("brief-hint").textContent = "失败："+e.message; }
}

function renderVariants(rows){
  const box = document.getElementById("variants");
  if(!rows.length){ box.innerHTML = '<div class="muted">还没有草稿，先在上面生成。</div>'; return; }
  box.innerHTML = rows.map(v=>{
    const c = v.compliance || {};
    const cap = (v.fields && v.fields.caption && v.fields.caption.text) || "";
    const tags = (v.fields && v.fields.hashtags) || [];
    const st = String(v.status||"");
    const cls = st==="approved" ? "ok" : (st==="pending_review" ? "warn" : "");
    const findings = (c.blocking||[]).map(x=>'<li>block · '+esc(x.rule)+'：'+esc(x.message)+'</li>').join("")
      + (c.warnings||[]).map(x=>'<li style="color:#B26A00">warn · '+esc(x.rule)+'：'+esc(x.message)+'</li>').join("");
    const must = (c.must_review_individually||[]);
    const mustHtml = must.length ? '<div class="muted">必须逐条复核：'+must.map(m=>tag(m,"warn")).join("")+'</div>' : "";
    const actions = [];
    if(st==="draft") actions.push(btn("提交待审", "submitStatus('"+v.id+"','submit')", "primary"));
    if(st==="pending_review"){
      actions.push(btn("通过","submitStatus('"+v.id+"','approve')","primary"));
      actions.push(btn("驳回","submitStatus('"+v.id+"','reject')"));
      actions.push(btn("改稿","revise('"+v.id+"')"));
    }
    if(st==="needs_revision") actions.push(btn("重新提交待审","submitStatus('"+v.id+"','submit')","primary"));
    if(st==="approved") actions.push(btn("排期","openSchedule('"+v.id+"')","primary"));
    return '<div class="item"><h3>'+esc(v.platform)+' · '+tag(st, cls)+tag(v.id)+'</h3>'
      + '<div class="muted">'+esc(cap.slice(0,220))+(cap.length>220?"…":"")+'</div>'
      + '<div style="margin-top:6px">'+tags.map(t=>tag(t)).join("")+'</div>'
      + mustHtml
      + (findings? '<ul class="findings">'+findings+'</ul>' : '<div class="muted">合规：无命中</div>')
      + '<div class="actions">'+actions.join("")+'</div></div>';
  }).join("");
}

function btn(text, onclick, cls){ return '<button class="'+(cls||"ghost")+'" onclick="'+onclick+'">'+esc(text)+'</button>'; }

async function submitStatus(id, action){
  let diff = null;
  if(action==="approve" || action==="reject" || action==="needs_revision"){
    const note = prompt("审批备注（会记进留痕 diff，可留空）","");
    if(note) diff = {note};
  }
  try{ await api("PATCH","/api/v1/variants/"+id+"/status",{action,diff}); await refresh(); }
  catch(e){ alert("失败："+e.message); }
}

function revise(id){
  const note = prompt("改稿意见（会记进留痕 diff）","");
  if(note===null) return;
  api("PATCH","/api/v1/variants/"+id+"/status",{action:"needs_revision",diff:{note}}).then(refresh).catch(e=>alert("失败："+e.message));
}

function openSchedule(id){
  const tzOptions = TZ.map(([off,label])=>'<option value="'+off+'">'+label+' '+off+'</option>').join("");
  showDialog("排期", '<label>本地时间（按所选时区）</label>'
    + '<input type="datetime-local" id="sch-at">'
    + '<label>时区</label><select id="sch-tz">'+tzOptions+'</select>'
    + '<div class="actions"><button class="primary" onclick="doSchedule(\''+id+'\')">加入排期并投递</button>'
    + '<button onclick="doSchedule(\''+id+'\',true)">按 best-time 自动选时间</button></div>'
    + '<div class="muted">时间必须带时区偏移，否则后端会拒绝（多时区排期会漂移）。</div>');
}

async function doSchedule(id, bestTime){
  const body = bestTime ? {at_best_time:true} : {};
  if(!bestTime){
    const at = document.getElementById("sch-at").value;
    const off = document.getElementById("sch-tz").value;
    if(!at){ alert("请选择时间"); return; }
    body.scheduled_at = at + ":00" + off;
  }
  try{ await api("POST","/api/v1/variants/"+id+"/schedule",body); document.getElementById("dlg").close(); await refresh(); }
  catch(e){ alert("失败："+e.message); }
}

function renderSchedules(rows, variants){
  const box = document.getElementById("schedules");
  if(!rows.length){ box.innerHTML = '<div class="muted">还没有排期。</div>'; return; }
  const vmap = {}; (variants||[]).forEach(v=>vmap[v.id]=v);
  box.innerHTML = rows.map(s=>{
    const job = s.job || {};
    const v = vmap[s.variant_id] || {};
    const st = String(s.status||"");
    const cls = st==="published" ? "ok" : (st==="failed"||st==="cancelled" ? "bad" : "warn");
    const actions = [
      btn("立即发布","publishNow('"+s.id+"')","primary"),
      btn("重排","reschedule('"+s.id+"')"),
      btn("取消","cancelSchedule('"+s.id+"')"),
      btn("半自动导出","showSemiAuto('"+s.id+"')")
    ].join("");
    return '<div class="item"><h3>'+esc(v.platform||"")+' · '+tag(st, cls)+tag(s.id)+'</h3>'
      + '<div class="muted">时间 '+esc(s.scheduled_at)+'（'+esc(s.timezone)+'）· 任务 '+(job.id?esc(job.id)+' · '+esc(job.status||""):"未投递")+'</div>'
      + '<div class="actions">'+actions+'</div></div>';
  }).join("");
}

async function publishNow(id){
  try{ await api("POST","/api/v1/schedules/"+id+"/publish",{}); await refresh(); }
  catch(e){ alert("失败："+e.message); }
}

async function cancelSchedule(id){
  const reason = prompt("取消原因（留痕）","") || "";
  try{ await api("PATCH","/api/v1/schedules/"+id,{action:"cancel",reason}); await refresh(); }
  catch(e){ alert("失败："+e.message); }
}

async function reschedule(id){
  const at = prompt("新的本地时间（形如 2026-10-21T10:00）","");
  if(!at) return;
  const off = prompt("时区偏移（如 +05:30）","+05:30");
  try{ await api("PATCH","/api/v1/schedules/"+id,{action:"reschedule",scheduled_at:at+":00"+off}); await refresh(); }
  catch(e){ alert("失败："+e.message); }
}

async function showSemiAuto(id){
  try{
    const b = await api("GET","/api/v1/schedules/"+id+"/semi-auto");
    showDialog("半自动导出 · "+b.platform,
      '<label>正文（复制到平台）</label><pre>'+esc(b.text)+'</pre>'
      + '<label>素材</label><pre>'+esc((b.media_paths||[]).join("\\n") || "（无）")+'</pre>'
      + '<label>检查清单</label><ul>'+(b.checklist||[]).map(x=>'<li>'+esc(x)+'</li>').join("")+'</ul>'
      + '<div class="muted">'+esc(b.note||"")+'</div>');
  }catch(e){ alert("失败："+e.message); }
}

function showDialog(title, html){
  document.getElementById("dlg-title").textContent = title;
  document.getElementById("dlg-body").innerHTML = html;
  document.getElementById("dlg").showModal();
}

async function screenSubject(){
  const subject = document.getElementById("f-subject").value.trim();
  const checked_by = document.getElementById("f-checker").value.trim() || "ops";
  if(!subject){ alert("请填主体名"); return; }
  try{
    const r = await api("POST","/api/v1/compliance/screen",{subject,checked_by});
    const s = r.screening;
    const cls = s.result==="clear" ? "ok" : (s.result==="hit" ? "bad" : "warn");
    document.getElementById("screen-result").innerHTML =
      '<div style="margin-top:10px">'+tag(s.result, cls)+tag("查了 "+s.lists_checked.length+" 份清单")+'</div>'
      + '<div class="muted">'+esc(s.disclaimer)+'</div>'
      + '<pre>'+esc(JSON.stringify(s.evidence,null,2))+'</pre>';
  }catch(e){ document.getElementById("screen-result").textContent = "失败："+e.message; }
}

function renderDashboard(d){
  const c = d.content || {}, p = d.publishing || {}, f = d.funnel || {};
  document.getElementById("dashboard").innerHTML =
    '<div class="muted">'+esc(d.kpi_basis)+'</div>'
    + '<div class="row" style="margin-top:10px">'
    + '<div class="card"><b>'+(c.variants_total||0)+'</b><span>变体总数</span></div>'
    + '<div class="card"><b>'+(p.published||0)+'</b><span>发布成功</span></div>'
    + '<div class="card"><b>'+(p.success_rate==null?"—":Math.round(p.success_rate*100)+"%")+'</b><span>成功率</span></div>'
    + '</div>'
    + '<div class="muted" style="margin-top:8px">触达公司 '+ (f.reached_companies==null?"待接数据源":f.reached_companies)
    + ' · 询盘 '+ (f.inquiries==null?"待接数据源":f.inquiries)
    + ' · 进入短名单 '+ (f.shortlisted==null?"待接数据源":f.shortlisted)+'</div>';
}

refresh().catch(e=>{ document.getElementById("variants").textContent = "加载失败："+e.message; });
setInterval(()=>refresh().catch(()=>{}), 15000);
</script>
</body>
</html>
"""
