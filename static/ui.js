const elem = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const fmt = value => new Intl.NumberFormat("id-ID").format(value);
const statuses = {ready:"可提交审批",partial:"仍有缺口",blocked:"已阻断",no_action:"无需调拨",infeasible:"当前无可执行调拨"};
const decisions = {pending:"待处理",approved:"已批准",rejected:"已驳回"};
let scenarios = {}, records = [], busy = false;

async function request(path, body) {
  const response = await fetch(path, body ? {
    method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)
  } : undefined);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function scenarioChanged() {
  const s = scenarios[elem("scenario").value];
  elem("hours").value = s.deadline_hours;
  elem("budget").value = s.budget;
  elem("snapshot").textContent = "库存快照已采集 " + s.snapshot_age + " 小时 · 合成演示数据";
}

function executionPanel(record) {
  const execution = record.execution;
  if (!execution) return "";
  const rows = execution.lines.map((line, index) => {
    const lane = esc(line.lane_id);
    const title = '<h3>' + lane + ' · 批准 ' + line.planned + ' 件</h3>';
    if (line.state === "received") return '<div class="leg">' + title + '<p>已签收 ' + line.received + ' 件</p></div>';
    const inTransit = line.state === "in_transit";
    const inputs = inTransit ?
      '<label>最终实收件数<input name="quantity" type="number" min="0" max="' + line.planned +
      '" value="' + line.planned + '" required></label><label>签收说明（短收必填）' +
      '<input name="note" maxlength="500"></label>' : '';
    return '<form class="leg" data-line="' + index + '">' + title + inputs +
      '<button type="submit">' + (inTransit ? '登记最终签收' : '登记模拟发运') + '</button></form>';
  }).join("");
  const summary = execution.complete ? '全部线路已签收 · 最终缺口 ' + execution.final_gap + ' 件' : '等待其余线路完成';
  const events = execution.events.map(e => '<li>' + esc(e.lane_id) + ' · ' +
    (e.action === "dispatch" ? '发运' : '签收') + ' ' + e.quantity + ' 件 · ' +
    esc(e.recorded.replace("T", " ")) + (e.note ? ' · ' + esc(e.note) : '') + '</li>').join("");
  return '<section class="panel"><h2>发运与签收</h2><p>累计实收 ' + execution.received +
    ' 件 · ' + summary + '</p>' + rows + (events ? '<details><summary>操作记录</summary><ol>' +
    events + '</ol></details>' : '') + '</section>';
}

async function submitShipment(event, record) {
  event.preventDefault();
  const form = event.currentTarget;
  const line = record.execution.lines[Number(form.dataset.line)];
  const receiving = line.state === "in_transit";
  const body = {id: record.id, lane_id: line.lane_id, action: receiving ? "receive" : "dispatch"};
  if (receiving) {
    body.quantity = form.elements.quantity.valueAsNumber;
    body.note = form.elements.note.value;
  }
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const updated = await request("/api/fulfillment", body);
    display({...record, ...updated});
    elem("notice").textContent = "履约记录已保存";
    await history();
  } catch (error) {
    elem("notice").textContent = error.message;
    button.disabled = false;
  }
}

function display(record) {
  const r = record.result, p = r.proposal;
  const warehouse = id => esc(r.warehouses[id] || id);
  const route = row => '<div class="route"><strong>' + warehouse(row.origin) + ' → ' + warehouse(row.destination) +
    '</strong><div>' + row.quantity + ' 件 · ' + fmt(row.cost) + ' IDR</div><div><small>预计到货（当地）</small>' + esc(row.eta.slice(5,16).replace("T"," ")) + '</div></div>';
  const cards = p ? '<div class="cards">' + [["目标调拨",r.target+" 件"],["可调到货",p.quantity+" 件"],["剩余缺口",p.uncovered+" 件"],["预计运费",fmt(p.cost)+" IDR"]]
    .map(([label,value])=>'<div class="stat"><span>'+label+'</span><strong>'+value+'</strong></div>').join("")+'</div>' : "";
  const audit = r.evidence.map(e=>'<details><summary>'+esc(e.title)+' · '+esc(e.id)+'</summary><p>'+esc(e.body)+'</p><p class="muted">生效 '+esc(e.effective)+' · 检索 '+esc(e.query)+' · BM25 '+e.score+'</p></details>').join("");
  const review = (r.review || []).map(x=>'<p class="warn">'+esc(x.source)+' '+esc(x.detail)+'</p>').join("");
  const baseline = p ? '<section class="panel"><h2>最低运费基线</h2><p class="muted">只按运费、现货与线路余量选货，随后独立复核</p>'+
    r.baseline.shipments.map(route).join("")+
    (r.baseline_review.length ? r.baseline_review.map(x=>'<p class="warn">'+esc(x.source)+' · '+esc(x.detail)+'</p>').join("") : '<p>基线通过规则复核。</p>')+'</section>' : "";
  const inventory = p ? '<details><summary>库存计算与线路候选</summary><div class="table-wrap"><table><thead><tr><th>仓库</th><th>可用库存</th><th>截止前需求</th><th>安全库存</th><th>可调</th></tr></thead><tbody>'+
    Object.entries(r.inventory).map(([id,s])=>'<tr><td>'+warehouse(id)+'</td><td>'+s.available+'</td><td>'+s.future_demand+'</td><td>'+s.safety_stock+'</td><td>'+s.transferable+'</td></tr>').join("")+
    '</tbody></table></div>'+p.candidates.map(c=>'<p>'+esc(c.id)+' · '+c.hours+' 小时 · 运费 '+fmt(c.unit_cost)+' IDR/件 · '+(c.reasons.length?esc(c.reasons.join("、")):"可选 "+c.max_available+" 件")+'</p>').join("")+'</details>' :"";
  const risk = r.service_risk;
  const service = risk && risk.peak_shortage ? '<p class="warn">按均匀需求估算，到货前仍可能累计缺货 ' +
    risk.peak_shortage + ' 件。审批时请考虑临时替代或加急。</p>' : "";
  const trace = r.trace.map(x=>'<span>'+esc(x.step)+' · '+x.ms+' ms</span>').join("");
  const notes = Object.entries(r.model_notes || {}).map(([name, note])=>'<details><summary>'+esc(name)+'</summary><p>'+esc(note)+'</p></details>').join("");
  const undecided = record.decision === "pending";
  const allowed = ["ready","partial"].includes(r.status);
  const approval = undecided ? '<label for="note">审批说明'+(r.status==="partial"?"（部分批准必填）":"")+'</label><textarea id="note" rows="2" maxlength="500"></textarea><div class="actions">'+
    (allowed?'<button type="button" id="approve">批准方案</button>':'')+
    '<button type="button" class="secondary" id="reject">驳回</button></div>' :
    '<p>'+esc(decisions[record.decision])+(record.note?" · "+esc(record.note):"")+'</p>';
  elem("result").innerHTML = '<section class="panel"><div class="title-row"><h2>'+esc(r.scenario.label)+'</h2><span class="pill">'+esc(statuses[r.status])+'</span></div>'+
    cards+service+(r.reason?'<p class="warn">'+esc(r.reason)+'</p>':'')+review+
    (p?p.shipments.map(route).join(""):'')+inventory+
    '<div class="trace">'+trace+'</div></section>'+baseline+
    '<section class="panel"><h2>决策依据</h2>'+audit+notes+'</section>'+
    '<section class="panel"><h2>模拟审批</h2>'+approval+'</section>'+executionPanel(record);
  elem("result").querySelectorAll("form[data-line]").forEach(form => {
    form.onsubmit = event => submitShipment(event, record);
  });
  if (elem("approve")) elem("approve").onclick = () => decide(record, "approved");
  if (elem("reject")) elem("reject").onclick = () => decide(record, "rejected");
}

async function decide(record, decision) {
  const buttons = ["approve","reject"].map(elem).filter(Boolean);
  buttons.forEach(button => button.disabled = true);
  try {
    const updated = await request("/api/decision",{id:record.id,decision,note:elem("note").value});
    display({...record,...updated});
    elem("notice").textContent = "审批已保存";
    await history();
  } catch(error) {
    elem("notice").textContent = error.message;
    buttons.forEach(button => button.disabled = false);
  }
}

async function history() {
  records = await request("/api/history");
  elem("history").innerHTML = records.map((r,i)=>'<button type="button" data-index="'+i+'">'+esc(r.result.scenario.label)+
    '<small>'+esc(decisions[r.decision])+' · '+esc(r.created.slice(5,16).replace("T"," "))+' UTC</small></button>').join("") ||
    '<p class="muted">暂无调拨记录</p>';
}

elem("history").addEventListener("click", event=>{
  const button = event.target.closest("button[data-index]");
  if (button) {display(records[Number(button.dataset.index)]);elem("notice").textContent="";}
});
elem("scenario").addEventListener("change",scenarioChanged);
elem("form").addEventListener("submit",async event=>{
  event.preventDefault();
  if (busy) return;
  busy = true;
  elem("submit").disabled = true;
  elem("notice").textContent = "正在检索条款并计算方案…";
  try {
    const record = await request("/api/plan",{scenario:elem("scenario").value,
      deadline_hours:Number(elem("hours").value),budget:Number(elem("budget").value)});
    display(record);
    elem("notice").textContent = "";
    await history();
  } catch(error) {elem("notice").textContent = error.message;}
  finally {busy=false;elem("submit").disabled=false;}
});

async function initialize() {
  try {
    const data = await request("/api/scenarios");
    scenarios = data.scenarios;
    if (data.live) elem("mode").textContent = "模拟数据 · DeepSeek 已启用";
    for (const [id, s] of Object.entries(scenarios)) elem("scenario").add(new Option(s.label,id));
    scenarioChanged();
    elem("submit").disabled = false;
    await history();
  } catch(error) {elem("notice").textContent=error.message;}
}
initialize();
