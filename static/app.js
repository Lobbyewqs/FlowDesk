const $ = (s) => document.querySelector(s);
const labels = {queued:'排队中',processing:'处理中',needs_info:'待补充',awaiting_approval:'待审核',completed:'已完成',manual:'转人工',rejected:'已驳回',failed:'执行失败'};
const roles = {admin:'管理员',agent:'客服',customer:'客户'};
const intents = {logistics:'物流咨询',return:'退货申请',exchange:'换货申请',other:'其他诉求'};
const samples = {exchange:'订单 A1024 的耳机左耳没声音，我想换货。',logistics:'订单 A1025 还没收到，请查一下快递。',expired:'订单 A1026 的音箱不想要了，我想退货。',missing:'我买的耳机坏了，希望换货。',return:'订单 A1028 的支架不想要了，请申请退货。'};
const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const badge = (s) => `<span class="badge ${escapeHtml(s)}">${escapeHtml(labels[s] || s)}</span>`;
const formatTime = (s) => s ? new Date(s).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}) : '';
let user=null, meta=null, tickets=[], selected=null, page=1, pages=1, total=0, filter='all', view='desk', busy=false, setup=false, detailKey='', stats={counts:{},jobs:{},total:0};
let authEpoch=0, polling=false;
const isStaff = () => user && user.role !== 'customer';
const customerName = (id) => meta?.customers.find(c=>c.id===id)?.name || id;

function showAuth() {
  authEpoch++;
  user=null; selected=null; detailKey='';
  clearWorkspace();
  if($('#create-dialog').open) $('#create-dialog').close();
  $('#app-shell').classList.add('hidden'); $('#auth-view').classList.remove('hidden');
}
function clearWorkspace() {
  tickets=[]; meta=null;
  $('#user-form').reset(); $('#create-form').reset();
  if($('#user-role').onchange) $('#user-role').onchange();
  ['ticket-list','stats','orders-table','jobs-table','users-table','policy-library'].forEach(id=>$(`#${id}`).innerHTML='');
  $('#detail').innerHTML='<div class="empty-detail"><div class="empty-symbol">↗</div><h2>选择一条工单</h2><p>创建工单或从列表中选择，查看处理进度。</p></div>';
}
async function api(path, options={}) {
  const response=await fetch(path,{headers:{'Content-Type':'application/json'},...options});
  if(response.status===204) return null;
  const data=await response.json();
  if(!response.ok) {
    if(response.status===401 && user) showAuth();
    throw new Error(typeof data.detail==='string' ? data.detail : '操作失败，请检查输入后重试。');
  }
  return data;
}
let toastTimer;
function toast(message,error=false) {
  $('#toast').textContent=message; $('#toast').className=`toast${error?' error':''}`;
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('#toast').classList.add('hidden'),4500);
}
async function perform(task) {
  if(busy) return;
  busy=true; $('#busy').classList.remove('hidden');
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  try {await task();} catch(error) {toast(error.message,true);}
  finally {busy=false; $('#busy').classList.add('hidden'); document.querySelectorAll('button').forEach(b=>b.disabled=false); renderPager();}
}
function renderPager() {
  $('#page-info').textContent=`${page} / ${pages} 页 · 共 ${total} 条`;
  $('#prev-page').disabled=page<=1; $('#next-page').disabled=page>=pages;
}
function renderStats() {
  const c=stats.counts;
  $('#nav-count').textContent=stats.total;
  $('#stats').innerHTML=[['全部工单',stats.total,'当前账号可见的全部工单','▤'],['等待人工输入',(c.needs_info||0)+(c.awaiting_approval||0),'待审核 / 待补充信息','◷'],['已完成处理',c.completed||0,'审核通过并归档','✓'],['需要关注',(c.manual||0)+(c.failed||0),'转人工 / 执行失败','↗']].map(([name,value,note,icon])=>`<div class="stat"><div class="stat-label">${name}</div><span class="stat-marker">${icon}</span><div class="stat-value">${String(value).padStart(2,'0')}</div><div class="stat-note">${note}</div></div>`).join('');
  $('#queue-health').classList.toggle('hidden',!isStaff());
  $('#queue-health').textContent=`后台队列：排队 ${stats.jobs.queued||0} · 执行 ${stats.jobs.running||0} · 完成 ${stats.jobs.done||0} · 失败 ${stats.jobs.failed||0}　 /　${stats.workers||0} 个执行线程 · 自动刷新`;
}
function renderList() {
  $('#ticket-list').innerHTML=tickets.length?tickets.map(t=>`<button class="ticket-item ${selected===t.id?'selected':''}" data-id="${escapeHtml(t.id)}"><div class="ticket-item-top"><span class="ticket-id">${escapeHtml(t.id)}</span>${badge(t.status)}</div><p class="ticket-title">${escapeHtml(t.description)}</p><div class="ticket-meta"><span>${escapeHtml(customerName(t.customer_id))} · ${escapeHtml(intents[t.state.intent]||'等待处理')}</span><time>${formatTime(t.created_at)}</time></div></button>`).join(''):'<div class="queue-empty">没有符合筛选条件的工单<br>点击「新建工单」开始体验</div>';
  document.querySelectorAll('.ticket-item').forEach(el=>el.onclick=()=>perform(()=>selectTicket(el.dataset.id,true)));
  renderPager();
}
async function refresh(poll=false) {
  const epoch=authEpoch;
  const [list,summary]=await Promise.all([api(`/api/tickets?page=${page}&status=${filter==='all'?'':filter}`),api('/api/stats')]);
  if(!user || epoch!==authEpoch) return;
  tickets=list.items; pages=list.pages; total=list.total; stats=summary;
  if(page>pages) {page=pages; return refresh(poll);}
  renderStats(); renderList();
  if(selected) await selectTicket(selected,!poll);
}
async function selectTicket(id,force=false) {
  const epoch=authEpoch;
  const t=await api(`/api/tickets/${encodeURIComponent(id)}`);
  if(!user || epoch!==authEpoch) return;
  selected=id; renderList();
  const key=`${id}:${t.status}:${t.updated_at}:${t.events.length}:${t.job?.status}:${t.job?.attempts}`;
  if(force || key!==detailKey) {renderDetail(t); detailKey=key;}
}
function renderDetail(t) {
  const s=t.state;
  const runMode=s.mode==='live'?`真实模型 · ${s.model||'未记录'}`:s.mode==='demo'?'演示规则 + 模板':'历史工单 · 未记录模式';
  const done=new Set(t.events.map(e=>e.node));
  if(!isStaff()) {
    if(s.intent) done.add('analyze');
    if(s.order) done.add('query_order');
    if(s.policies?.length) done.add('search_policy');
    if(s.rule_reason) done.add('validate');
    if(['completed','rejected'].includes(t.status)) done.add('review');
    if(t.status==='completed') done.add('execute');
  }
  const stages=[['analyze','理解诉求'],['query_order','查询订单'],['search_policy','检索政策'],['validate','规则校验'],['review','人工审核'],['execute','执行归档']];
  const flow=stages.map(([node,label],i)=>`<div class="flow-step ${node==='review'&&t.status==='awaiting_approval'?'current':done.has(node)?'done':''}"><span>${done.has(node)?'✓':String(i+1).padStart(2,'0')}</span><b>${label}</b></div>`).join('');
  const order=s.order?`<div class="detail-section"><h3 class="section-label">订单信息 <span>订单归属已验证</span></h3><div class="order-summary"><div><small>订单编号</small><strong>${escapeHtml(s.order.id)}</strong></div><div><small>商品名称</small><strong>${escapeHtml(s.order.product)}</strong></div><div><small>订单金额</small><strong>¥ ${(s.order.amount/100).toFixed(2)}</strong></div></div></div>`:'';
  const policies=s.policies?.length?`<div class="detail-section"><h3 class="section-label">政策依据 <span>本地 BM25 · 版本引用</span></h3>${s.policies.map(p=>`<article class="policy-item"><h3>${escapeHtml(p.title)}</h3><p>${escapeHtml(p.content)}</p><div class="policy-source">${escapeHtml(p.source)} · ${escapeHtml(p.id)} / ${escapeHtml(p.version)} · 匹配分 ${p.score}</div></article>`).join('')}</div>`:'';
  let operation='';
  if(t.status==='needs_info') operation=`<div class="detail-section info-box"><p>${escapeHtml(s.pause?.message||'请补充订单信息。')}</p><form id="info-form" class="inline-form"><input id="info-text" aria-label="补充订单信息" required minlength="2" maxlength="1000" placeholder="例如：订单号 A1024"><button class="button primary small">补充并继续</button></form></div>`;
  if(t.status==='awaiting_approval') operation=isStaff()?`<div class="detail-section approval-box"><h3 class="section-label">审核回复与处理方案 <span>可编辑草稿</span></h3><textarea id="reply-text" aria-label="回复草稿" maxlength="4000" rows="4">${escapeHtml(s.draft)}</textarea><input id="review-note" aria-label="审核备注" maxlength="1000" placeholder="内部审核备注（驳回时必填）"><div class="approval-actions"><button id="reject" class="button danger small">驳回申请</button><button id="approve" class="button primary small">${s.intent==='logistics'?'批准并归档回复':'批准并创建模拟售后单'} ↗</button></div></div>`:'<div class="detail-section info-box"><p>正在等待客服审核。审核通过后，可在此查看回复与处理结果。</p></div>';
  if(t.status==='completed') operation=`<div class="detail-section"><h3 class="section-label">已审核回复 <span>本地归档</span></h3><div class="final-reply">${escapeHtml(s.reply)}</div>${t.action?`<p class="action-result">✓ 模拟${s.intent==='exchange'?'换货':'退货'}单 ${escapeHtml(t.action.id)} 已创建，尚未发生真实退款或发货。</p>`:'<p class="action-result">✓ 物流回复已审核归档。</p>'}</div>`;
  if(t.status==='rejected') operation=`<div class="detail-section rule-box warning">申请已驳回。${escapeHtml(s.decision?.note||'请联系客服了解详情。')}</div>`;
  if(['failed','processing','queued'].includes(t.status)) {
    const active=t.job || t.status!=='failed';
    operation=`<div class="detail-section info-box"><p>${active?'后台任务已接收，可离开页面，完成后会自动更新。':escapeHtml(s.error||'处理未完成，请联系客服或重试。')}</p>${isStaff() && !t.job && ['failed','processing'].includes(t.status)?'<button class="button primary small" id="retry">从检查点重试</button>':''}</div>`;
  }
  const timeline=isStaff()?`<div class="timeline"><details><summary>处理过程 <span>${t.events.length} 条记录</span></summary><div class="events">${t.events.map(e=>`<div class="event"><h4><span>${escapeHtml(e.title)}</span><time>${formatTime(e.created_at)} · ${e.duration_ms}ms</time></h4><p>${escapeHtml(e.detail)}</p></div>`).join('')}</div></details></div>`:'';
  $('#detail').innerHTML=`<div class="detail-header"><div class="detail-topline"><span class="ticket-id">${escapeHtml(t.id)}${t.batch_id?' · 演示批次':''}</span>${badge(t.status)}</div><h2 class="detail-heading">${escapeHtml(intents[s.intent]||'售后诉求处理')}</h2><div class="detail-caption">${escapeHtml(customerName(t.customer_id))} · ${escapeHtml(t.customer_id)}　/　${formatTime(t.created_at)}</div></div><div class="detail-body"><h3 class="section-label">客户诉求</h3><div class="customer-message">${escapeHtml(s.description||t.description)}</div><div class="flow-steps">${flow}</div>${order}${s.rule_reason?`<div class="rule-box ${t.status==='manual'?'warning':''}">${escapeHtml(s.rule_reason)}</div>`:''}${policies}${operation}${timeline}<div class="usage-line"><span>模式：${escapeHtml(runMode)}</span>${isStaff()?`<span>输入 ${s.input_tokens||0} / 输出 ${s.output_tokens||0} tokens</span>`:''}<span>后台任务 · 检查点持久化</span></div></div>`;
  if($('#info-form')) $('#info-form').onsubmit=e=>{e.preventDefault(); const text=$('#info-text').value; perform(()=>mutate('information',{text}));};
  if($('#approve')) $('#approve').onclick=()=>approve(true);
  if($('#reject')) $('#reject').onclick=()=>approve(false);
  if($('#retry')) $('#retry').onclick=()=>perform(()=>mutate('retry'));
}
function approve(approved) {
  const note=$('#review-note').value.trim(),reply=$('#reply-text').value.trim();
  if(!approved && !note) {toast('请填写驳回原因。',true); return;}
  perform(()=>mutate('approval',{approved,note,reply}));
}
async function mutate(kind,body) {
  await api(`/api/tickets/${selected}/${kind}`,{method:'POST',...(body?{body:JSON.stringify(body)}:{})});
  await refresh(); toast('操作已提交后台，处理完成后自动更新。');
}
async function loadOrders() {
  const orders=await api(`/api/orders?customer_id=${$('#order-customer').value}`);
  $('#orders-table').innerHTML=orders.map(o=>`<tr><td>${escapeHtml(o.id)}</td><td>${escapeHtml(o.product)}</td><td>¥ ${(o.amount/100).toFixed(2)}</td><td>${o.status==='delivered'?'已签收':'运输中'}</td><td>${escapeHtml(o.tracking)}</td></tr>`).join('');
}
async function loadJobs() {
  const epoch=authEpoch;
  const data=await api('/api/jobs');
  if(!user || epoch!==authEpoch) return;
  $('#job-summary').textContent=`最近 50 条任务 · 排队 ${data.summary.queued||0} / 执行 ${data.summary.running||0} / 完成 ${data.summary.done||0} / 失败 ${data.summary.failed||0}`;
  const jobLabels={queued:'排队中',running:'执行中',done:'已完成',failed:'失败'};
  $('#jobs-table').innerHTML=data.items.map(j=>`<tr><td><small>${escapeHtml(j.id)}</small><br><button class="job-link" data-ticket="${escapeHtml(j.ticket_id)}">${escapeHtml(j.ticket_id)}</button></td><td>${escapeHtml({start:'初次处理',information:'补充信息',approval:'审批执行',retry:'手动重试'}[j.kind]||j.kind)}</td><td>${jobLabels[j.status]}</td><td>${j.attempts} / ${j.max_attempts}</td><td>${escapeHtml(j.last_error||'—')}</td></tr>`).join('');
  document.querySelectorAll('.job-link').forEach(b=>b.onclick=()=>perform(async()=>{showView('desk'); await selectTicket(b.dataset.ticket,true);}));
}
async function loadUsers() {
  const users=await api('/api/admin/users');
  $('#users-table').innerHTML=users.map(u=>`<tr><td>${escapeHtml(u.username)}</td><td>${roles[u.role]}</td><td>${escapeHtml(u.customer_id||'—')}</td><td>${u.active?'启用':'停用'}</td><td>${u.id===user.id?'当前账号':`<button class="button small user-active" data-id="${u.id}" data-active="${u.active?0:1}">${u.active?'停用':'启用'}</button>`}</td></tr>`).join('');
  document.querySelectorAll('.user-active').forEach(b=>b.onclick=()=>perform(async()=>{await api(`/api/admin/users/${b.dataset.id}`,{method:'PATCH',body:JSON.stringify({active:b.dataset.active==='1'})}); await loadUsers();}));
}
function showView(name) {
  view=name;
  ['desk','policies','orders','jobs','users'].forEach(n=>$(`#${n}-view`).classList.toggle('hidden',n!==name));
  document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.view===name));
  const names={desk:['工单工作台','每一条诉求，都有迹可循。','从理解问题到审核执行，让售后处理清晰、有序。'],policies:['售后政策库','每一项建议，都有依据。','固定版本的规则，为检索与审核提供共同依据。'],orders:['演示订单','从订单开始，核对每一步。','所有订单和售后操作均为本地模拟。'],jobs:['后台任务','让处理持续，让进度可见。','持久化队列、租约续期、自动重试与恢复。'],users:['账号管理','每个角色，各司其职。','管理员管理账号，客服审核执行，客户查看自己的进度。']};
  $('#page-label').textContent=names[name][0]; $('#page-title').textContent=names[name][1]; $('#page-subtitle').textContent=names[name][2];
}
async function enter() {
  authEpoch++; clearWorkspace();
  meta=await api('/api/meta'); user=meta.user;
  $('#auth-form').reset(); $('#auth-view').classList.add('hidden'); $('#app-shell').classList.remove('hidden');
  $('#profile-name').innerHTML=`${escapeHtml(user.username)}<small>${roles[user.role]}</small>`;
  $('#profile-avatar').textContent=user.username[0].toUpperCase();
  document.querySelectorAll('[data-permission]').forEach(el=>el.classList.toggle('hidden',el.dataset.permission==='admin'?user.role!=='admin':!isStaff()));
  const customerOptions=meta.customers.map(c=>`<option value="${c.id}">${escapeHtml(c.name)} · ${c.id}</option>`).join('');
  $('#customer').innerHTML=customerOptions; $('#order-customer').innerHTML=customerOptions;
  $('#mode-label').textContent=meta.mode==='live'?`● 真实模型 · ${meta.model}`:'● 演示模式 · 无模型调用';
  $('#connection-label').textContent='服务已连接'; $('#mode-description').textContent='持久化队列 · 自动处理与状态更新';
  $('#policy-library').innerHTML=meta.policies.map(p=>`<article class="panel library-card"><span class="badge">${escapeHtml(p.id)} · ${escapeHtml(p.version)}</span><h2>${escapeHtml(p.title)}</h2><p>${escapeHtml(p.content)}</p><div class="policy-source">${escapeHtml(p.source)}</div></article>`).join('');
  page=1; filter='all'; selected=null; detailKey=''; showView('desk'); await refresh();
  if(tickets.length) await selectTicket(tickets[0].id,true);
}
async function initialize() {
  try {
    setup=(await api('/api/auth/setup')).required;
    $('#auth-title').textContent=setup?'创建管理员账号':'登录工作空间';
    $('#auth-description').textContent=setup?'首次使用：设置管理员账号与密码，已有工单会完整保留。':'使用你的账号查看和处理售后工单。';
    $('#auth-submit').textContent=setup?'创建并进入工作台':'登录';
    $('#confirm-wrap').classList.toggle('hidden',!setup); $('#auth-confirm').required=setup;
    $('#auth-password').autocomplete=setup?'new-password':'current-password';
    if(!setup) {try {await api('/api/auth/me'); await enter();} catch(error) {showAuth();}}
  } catch(error) {$('#auth-error').textContent=error.message;}
}
$('#auth-form').onsubmit=e=>{
  e.preventDefault();
  if(setup && $('#auth-password').value!==$('#auth-confirm').value) {$('#auth-error').textContent='两次密码不一致'; return;}
  const body={username:$('#auth-username').value,password:$('#auth-password').value};
  perform(async()=>{await api(`/api/auth/${setup?'setup':'login'}`,{method:'POST',body:JSON.stringify(body)}); setup=false; await enter();});
};
$('#logout').onclick=()=>perform(async()=>{await api('/api/auth/logout',{method:'POST'}); showAuth(); $('#auth-confirm').required=false; $('#confirm-wrap').classList.add('hidden'); $('#auth-title').textContent='登录工作空间'; $('#auth-submit').textContent='登录'; $('#auth-password').autocomplete='current-password'; $('#auth-description').textContent='使用你的账号查看和处理售后工单。';});
document.querySelectorAll('.nav-item').forEach(el=>el.onclick=()=>perform(async()=>{showView(el.dataset.view); if(view==='orders') await loadOrders(); if(view==='jobs') await loadJobs(); if(view==='users') await loadUsers();}));
document.querySelectorAll('.filter').forEach(el=>el.onclick=()=>perform(async()=>{filter=el.dataset.filter; page=1; document.querySelectorAll('.filter').forEach(b=>b.classList.toggle('active',b===el)); await refresh();}));
$('#refresh').onclick=()=>perform(()=>refresh());
$('#prev-page').onclick=()=>perform(async()=>{page--; await refresh();}); $('#next-page').onclick=()=>perform(async()=>{page++; await refresh();});
$('#order-customer').onchange=()=>perform(loadOrders);
const openDialog=()=>{$('#create-dialog').showModal(); $('#description').focus();};
$('#new-ticket').onclick=openDialog; $('#empty-create').onclick=openDialog;
$('#close-dialog').onclick=()=>$('#create-dialog').close();
document.querySelectorAll('[data-sample]').forEach(el=>el.onclick=()=>{$('#description').value=samples[el.dataset.sample];});
$('#create-form').onsubmit=e=>{e.preventDefault(); const body={customer_id:$('#customer').value,description:$('#description').value}; perform(async()=>{const t=await api('/api/tickets',{method:'POST',body:JSON.stringify(body)}); selected=t.id; page=1; $('#create-dialog').close(); $('#create-form').reset(); showView('desk'); await refresh(); toast('工单已进入后台队列，可继续其他操作。');});};
$('#user-role').onchange=()=>$('#user-customer').disabled=$('#user-role').value!=='customer'; $('#user-role').onchange();
$('#user-form').onsubmit=e=>{e.preventDefault(); const body={username:$('#user-username').value,password:$('#user-password').value,role:$('#user-role').value,customer_id:$('#user-role').value==='customer'?$('#user-customer').value:null}; perform(async()=>{await api('/api/admin/users',{method:'POST',body:JSON.stringify(body)}); $('#user-form').reset(); $('#user-role').onchange(); await loadUsers(); toast('账号已创建。');});};
$('#seed-batch').onclick=()=>perform(async()=>{const batch=await api('/api/admin/demo-batches',{method:'POST',body:JSON.stringify({count:200})}); await loadJobs(); await refresh(); toast(`已提交 ${batch.count} 条演示工单，后台自动处理。`);});
setInterval(async()=>{if(!user||busy||polling||document.hidden) return; polling=true; try {await refresh(true); if(view==='jobs'&&user&&isStaff()) await loadJobs();} catch(error) {if(user) toast(error.message,true);} finally {polling=false;}},2000);
initialize();
