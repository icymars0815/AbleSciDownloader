"use strict";
/* 科研通下载器 (KYTDL) local UI. Talks only to the local app backend (same origin). */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
// This is a local app; never describe it (or the source site) as a web page.
const human = s => String(s ?? '').replaceAll('网站', '科研通').replaceAll('官网', '科研通').replaceAll('网页', '求助详情').replaceAll('开放获取', '免费全文').replaceAll('流水线', '自动流程');

/* ---------- Bootstrap helpers: switches, segmented controls, modals ---------- */
const swBtn = (attrs, on, label, id) => `<div class="form-switch ios"><input class="form-check-input" type="checkbox" role="switch" ${id ? `id="${id}"` : ''} aria-checked="${on ? 'true' : 'false'}" aria-label="${esc(label)}" ${on ? 'checked' : ''} ${attrs}></div>`;
/* Server-backed switch: the state shown always comes from the backend (the click is cancelled), so a switch that needs a confirmation does not flip until it is confirmed. */
const sw = (act, id, on, label, extra = '') => swBtn(`data-action="${act}"${id != null ? ` data-id="${esc(id)}"` : ''} ${extra}`, on, label);
/* Form switch: toggles locally and is read when the form is saved. */
const swLocal = (id, on, label) => swBtn('', on, label, id);
const isOn = id => !!$(id)?.checked;
const seg = (label, opts, current, act, extra = '') => `<div class="btn-group" role="radiogroup" aria-label="${esc(label)}">${opts.map(([k, n]) => `<input type="radio" class="btn-check" name="seg-${act}" id="seg-${act}-${esc(k)}" autocomplete="off" ${k === current ? 'checked' : ''} data-action="${act}" data-id="${esc(k)}" ${extra}><label class="btn btn-outline-primary btn-sm" for="seg-${act}-${esc(k)}">${n}</label>`).join('')}</div>`;
const segLocal = (id, label, opts, current, disabledKeys = []) => `<div class="btn-group" id="${id}" role="radiogroup" aria-label="${esc(label)}">${opts.map(([k, n]) => `<input type="radio" class="btn-check" name="${id}" id="${id}-${esc(k)}" value="${esc(k)}" autocomplete="off" ${k === current ? 'checked' : ''} ${disabledKeys.includes(k) ? 'disabled' : ''}><label class="btn btn-outline-primary btn-sm" for="${id}-${esc(k)}">${n}</label>`).join('')}</div>`;
/* Give plain markup produced by the render functions their Bootstrap classes. */
function bsify(root) {
  root.querySelectorAll('button').forEach(b => {
    if (/\b(btn|btn-close|nav-link)\b/.test(b.className)) return;
    const c = b.classList, kind = c.contains('primary') ? 'btn-primary' : c.contains('danger') ? 'btn-outline-danger' : c.contains('quiet') ? 'btn-link' : 'btn-outline-secondary';
    c.remove('primary', 'danger', 'quiet'); c.add('btn', kind, 'btn-sm');
  });
  root.querySelectorAll('input:not([class])').forEach(i => i.classList.add(i.type === 'checkbox' || i.type === 'radio' ? 'form-check-input' : 'form-control'));
  root.querySelectorAll('textarea:not([class])').forEach(i => i.classList.add('form-control'));
  root.querySelectorAll('select:not([class])').forEach(i => i.classList.add('form-select'));
  root.querySelectorAll('table.plain, table.aitable').forEach(t => t.classList.add('table', 'table-sm'));
}
const mOpen = id => $(id).classList.contains('show');
const mShow = id => { if (!mOpen(id)) bootstrap.Modal.getOrCreateInstance($(id)).show(); };
const mHide = id => { bootstrap.Modal.getInstance($(id))?.hide(); };
const setModal = (id, html) => { const c = $(id).querySelector('.modal-content'); c.innerHTML = html; bsify(c); };
const modalHead = (title, closeAction, idAttr = '') => `<div class="modal-header"><h3 class="modal-title h5" ${idAttr}>${esc(title)}</h3>${closeAction ? `<button type="button" class="btn-close" data-action="${closeAction}" aria-label="关闭"></button>` : ''}</div>`;

const workflowNames = { queued: '排队', downloading: '下载中', review: '待审核', downloaded: '已下载', done: '已完结', rejected: '已驳回', adopted: '已采纳' };
const checkNames = { match: '匹配', partial: '部分匹配', unverified: '未核实', difference: '疑似差异', review: '待审核' };
const stageNames = {
  attention: '需处理', downloading: '下载中', done: '已完结', awaiting_decision: '待采纳 / 驳回', oa_review: '免费全文待确认',
  fulfilled: '已有应助', submitted: '已求助 · 等待应助', queued: '尚未求助'
};
const oaNames = { found: '有免费全文链接', downloaded: '免费全文已下载', not_oa: '无免费全文', failed: '免费全文下载失败', needs_human: '需人工验证', skipped: '改走科研通' };
const RULES_VERSION = '2026-10-19-v1';
const versionNames = { publishedVersion: '正式版', acceptedVersion: '接受稿', submittedVersion: '预印本' };
const fmtSize = n => { n = Number(n) || 0; for (const u of ['B', 'KB', 'MB', 'GB']) { if (n < 1024 || u === 'GB') return (u === 'B' ? n.toFixed(0) : n.toFixed(1)) + ' ' + u; n /= 1024; } };
const fmtDur = sec => { sec = Math.max(0, Math.round(sec)); if (sec < 60) return sec + ' 秒'; if (sec < 3600) return Math.floor(sec / 60) + ' 分 ' + (sec % 60) + ' 秒'; return Math.floor(sec / 3600) + ' 小时 ' + Math.floor(sec % 3600 / 60) + ' 分'; };
function versionChip(oa) {
  const v = oa && oa.version;
  if (!v) return '';
  const label = versionNames[v] || v;
  const warn = v !== 'publishedVersion' ? ' warnchip' : '';
  return `<span class="tag${warn}" title="免费全文的版本。接受稿、预印本可能与正式版存在差异，请自行判断是否适用${oa.cached ? '。本次结果来自本机缓存' : ''}">版本：${esc(label)}</span>`;
}
function retractionWarning(i) {
  const bad = i.metadata_ref?.retraction?.retracted === true || i.verification?.retraction?.retracted === true || i.metadata?.retraction?.retracted === true;
  return bad ? '<div class="verdict warning"><b>该文献已被撤稿或有更正通知</b><div>请先在出版社页面核实，再决定是否使用该文件。</div></div>' : '';
}
function downloadInfo(i) {
  const p = i.download_progress, f = i.download_failure;
  let html = '';
  if (i.status === 'downloading' && p) {
    const bits = [];
    if (p.line) bits.push('线路：' + esc(p.line));
    if (p.total) bits.push(`${esc(fmtSize(p.received))} / ${esc(fmtSize(p.total))}（${esc(p.percent)}%）`);
    if (p.speed_bps) bits.push('速度 ' + (p.speed_bps / 1024).toFixed(1) + ' KB/s');
    if (p.eta_seconds != null) bits.push('预计还需 ' + esc(fmtDur(p.eta_seconds)));
    html += `<div class="verdict"><b>下载中</b>${p.plan ? `<div>${esc(p.plan)}</div>` : ''}${bits.length ? `<div>${bits.join(' · ')}</div>` : ''}${p.total ? `<progress max="100" value="${esc(p.percent)}" aria-label="下载进度"></progress>` : ''}</div>`;
  }
  if (i.line_probe && i.status === 'downloading') html += `<div class="hint">已测试各线路，选用线路 ${esc(i.line_probe.chosen)}。</div>`;
  if (i.status === 'retrying' && i.download_retry) html += `<div class="verdict"><b>自动重试中（第 ${esc(i.download_retry.count)} 次，预计 ${esc(String(i.download_retry.next_at || '').slice(11, 16))} 重试）</b><div>上次未完成：${esc(i.download_retry.last_reason || '')}。普通线路已失败 ${esc(i.normal_failures || 0)} 次，高速通道已失败 ${esc(i.highspeed_failures || 0)} 次。可点击“重试”立即重新开始，失败次数将清零。</div></div>`;
  if (f && i.status === 'attention') html += `<div class="verdict warning"><b>${esc(f.label)}</b><div>建议：${esc(f.advice)}</div></div>`;
  return html;
}
function localFileRow(i) {
  if (!i.pdf) return '';
  const name = i.original_filename || i.local_filename;
  return `<div class="deltag">${name ? `<span>文件名（沿用科研通原名）：${esc(name)}</span>` : ''}<button data-action="open-file" data-id="${esc(i.id)}|pdf">打开 PDF</button><button data-action="open-file" data-id="${esc(i.id)}|folder">打开所在文件夹</button></div>`;
}
/* A malformed URL from a third-party record must never break rendering. */
function urlHost(u) {
  try { return new URL(String(u)).host || String(u).slice(0, 60); } catch (e) { return String(u == null ? '' : u).slice(0, 60); }
}
function uncertainPublishInfo(i) {
  const st = i.publish_action?.status;
  if (!['attempted', 'uncertain'].includes(st)) return '';
  const done = i.publish_action?.user_verified ? '<div class="hint">已核实科研通上已发出，将在下次同步时自动对应。</div>' : '';
  return `<div class="verdict warning"><b>发布结果不确定</b><div>求助可能已发出，也可能未发出。请先在科研通“我的求助”中核实，再选择下方操作。软件不会自动重试，以免重复扣分。</div>${done}<div class="actions"><button data-action="reset-publish" data-id="${esc(i.id)}">核实：未发出，重置</button><button data-action="confirm-published" data-id="${esc(i.id)}">核实：已发出</button></div></div>`;
}
function handoverInfo(i) {
  if (!i.handover || i.status !== 'attention') return '';
  const waiting = i.handover.waiting_sync ? '<div class="hint">已记录，等待同步确认结果。</div>' : '';
  return `<div class="verdict warning"><b>科研通弹窗遮挡了${esc(i.handover.kind || '操作')}按钮（未提交）</b><div>请在浏览器中手动${esc(i.handover.kind || '处理')}。完成后点击“已在科研通处理”；如未处理，可点击“重新尝试”，回到待处理状态。</div>${waiting}<div class="actions"><button data-action="handover-done" data-id="${esc(i.id)}">已在科研通处理</button><button data-action="handover-retry" data-id="${esc(i.id)}">重新尝试采纳 / 驳回</button></div></div>`;
}
function publishInfo(i) {
  const unsure = uncertainPublishInfo(i);
  if (unsure) return unsure;
  const c = i.publish_check;
  if (!c || c.kind === 'published') return '';
  const rows = [['科研通识别的标题', c.title], ['DOI', c.doi], ['相似度', c.similarity != null ? c.similarity + '%' : ''], ['期刊', c.journal], ['类型', c.type], ['最低悬赏', c.min_reward != null ? c.min_reward + ' 积分' : ''], ['公开记录标题', c.reference_title]]
    .filter(r => r[1] !== '' && r[1] != null).map(r => `<div>${esc(r[0])}：${esc(r[1])}</div>`).join('');
  const shot = c.screenshot ? `<a href="/api/shot/${encodeURIComponent(i.id)}" target="_blank" rel="noopener">查看页面截图 ↗</a>` : '';
  const ack = i.status === 'attention' && !['attempted', 'uncertain'].includes(i.publish_action?.status) && (i.publish_action?.status !== 'confirmed' || i.assist_url) ? `<button data-action="ack-item" data-id="${esc(i.id)}">自行处理，不再自动发布</button>` : '';
  return `<div class="verdict warning"><b>求助未提交（未扣积分）</b><div>${esc(human(c.reason || ''))}</div>${rows}${shot ? `<div>${shot}</div>` : ''}${ack ? `<div class="actions">${ack}</div>` : ''}</div>`;
}

const SETTINGS_TABS = [
  ['privacy', '隐私'], ['donate', '捐助'], ['ai', 'AI 辅助'], ['mcp', 'MCP'],
  ['download', '下载'], ['reject', '应助校验'], ['pipeline', '自动流程'], ['notify', '通知'], ['accounts', '账户'], ['logs', '日志反馈'], ['about', '关于']
];

let filter = 'all', lastTasks = '', lastAccounts = '', lastPanel = '';
let importRecords = null, previewSequence = 0, previewTimer, noticeTimer;
let rejectTarget = null, aiTarget = null, mcpConfig = null, settingsTab = 'privacy';
const AI_OFF_HINT = '请先在设置中开启 AI 辅助';
const aiOn = () => !!state.ai?.enabled;
let importUnparsed = [], organizeCtx = null, disclaimerShown = false;
const logsState = { errors: null, selected: new Set(), report: null, reportId: '', analysis: null, consent: false };

function notice(message) {
  $('toastBody').textContent = human(message);
  bootstrap.Toast.getOrCreateInstance($('toast'), { delay: 7000 }).show();
}

async function post(path, body = {}) {
  const response = await fetch('/api/' + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Demo-Token': TOKEN },
    body: JSON.stringify(body)
  });
  let result = {};
  try { result = await response.json(); } catch (e) { /* non-JSON error body */ }
  if (!response.ok) throw Error(result.error || '操作未完成');
  return result;
}

async function action(path, body = {}) {
  try { await post(path, body); await refresh(); return true; }
  catch (e) { notice(e.message); return false; }
}

/* ---------- small dialog (confirmations, login, reject, AI pages) ---------- */
function openDialog(title, html) {
  setModal('dialog', `${modalHead(title, 'close-dialog')}<div class="modal-body dialogbody">${html}</div>`);
  mShow('dialog');
}
const closeDialog = () => mHide('dialog');
const settingsOpen = () => mOpen('settings');
const findItem = id => (state.items || []).find(i => i.id === id);

/* ---------- per-item blocks ---------- */
function bookInfo(item) {
  const b = item.verification?.bibliography, m = item.metadata;
  if (!b && !m) return '';
  const authors = b?.reference_authors || m?.authors || [];
  const abstract = b?.reference_abstract || m?.abstract || '';
  const sources = (b?.sources || m?.sources || []).join(' + ') || '暂不可用';
  const summary = b
    ? `<div>标题：${esc(checkNames[b.title_status] || '未核实')} · 作者：${esc(checkNames[b.author_status] || '未核实')}（${b.matched_authors?.length || 0}/${authors.length}） · 摘要：${esc(checkNames[b.abstract_status] || '未核实')}${b.abstract_score !== undefined ? ' · 相似度 ' + esc(b.abstract_score) + '%' : ''}</div>`
    : '<div>已获取参考记录，保存 PDF 后可进行比对。</div>';
  return `<div class="verdict"><b>公开书目 · ${esc(sources)}</b>${summary}<details><summary>查看参考作者与摘要</summary><p>标题：${esc(b?.reference_title || m?.title || '未提供')}</p><p>作者：${esc(authors.join('；') || '来源未提供')}</p><p>${esc(abstract || '来源未提供摘要（不视为差异）')}</p></details></div>`;
}

function rejectionInfo(item) {
  const v = item.verification, r = item.rejection;
  if (!v || v.status === 'identity_match') return '';
  const status = ({ confirmed: '已确认', attempted: '已尝试，请检查', uncertain: '结果未确认，不自动重试' })[item.reject_action?.status] || '';
  return `<div class="verdict warning"><b>需人工判断</b><div>${esc(human(r?.reason || v.reason))}</div>${r?.eligible && !item.reject_action ? `<div class="actions"><button data-action="reject" data-id="${esc(item.id)}" class="danger">核对并驳回…</button></div><div class="hint">${esc(state.reject_rules?.guidance || '')}</div>` : ''}${item.reject_action ? `<p>驳回状态：${esc(status)}</p>` : ''}</div>`;
}

function aiInfo(item) {
  const a = item.ai_assistance;
  if (!a) return '';
  return `<div class="verdict"><b>AI 辅助识别 · 需人工复核</b><details><summary>查看识别结果（不影响自动驳回）</summary><p>标题：${esc(a.extracted?.title)}</p><p>作者：${esc((a.extracted?.authors || []).join('；'))}</p><p>DOI：${esc(a.extracted?.doi)}</p><p>${esc(a.extracted?.abstract)}</p><p class="hint">${esc(a.model)} · 所选页：${esc((a.pages || []).join('、'))}</p></details></div>`;
}

function taskHtml(i, busy) {
  const v = i.verification, w = i.workflow || 'queued', id = esc(i.id);
  const pipe = state.pipeline;
  const gateLocked = !!(pipe && pipe.gate?.blocked && pipe.tier !== 'batch');
  const src = i.source === 'demo' ? '演示样例' : i.source === 'website' ? '我的求助 · ' + (i.website_status || '') : '本地导入';
  const stageChip = i.stage && i.source !== 'demo'
    ? `<span class="tag stage-${esc(i.stage)}" title="当前进度">${esc(stageNames[i.stage] || i.stage)}</span>` : '';
  const unconfirmed = i.source === 'import' && i.stage === 'queued' && i.batch_id && !i.request_confirmed
    ? '<span class="tag" title="在确认清单中勾选并确认后，才会向科研通求助">未确认求助</span>' : '';
  const dl = i.deletion || {};
  const gone = dl.state === 'deleted' && !i.pdf;
  const oa = i.oa_check, oaChip = oa && oaNames[oa.status]
    ? `<span class="tag oa-${esc(oa.status)}" title="${esc(oa.oa_status ? '免费类型：' + oa.oa_status : '免费全文检查')}${oa.source ? ' · 来源：' + esc(oa.source) : ''}">${esc(oaNames[oa.status])}</span>` : '';
  const btn = (act, text, extra = '') => `<button data-action="${act}" data-id="${id}" ${extra}>${text}</button>`;
  const dis = busy ? 'disabled' : '';
  const buttons = [];
  if (i.pdf) {
    buttons.push(`<a href="/pdf/${id}" target="_blank" rel="noopener">查看 PDF ↗</a>`, btn('verify', '核对 PDF'), btn('ai-recognize', 'AI 识别', aiOn() ? '' : `disabled title="${AI_OFF_HINT}"`));
    if (i.source === 'website' && !i.reject_action && i.stage === 'awaiting_decision') buttons.push(btn('reject', '驳回应助…', 'class="danger"'));
  } else {
    buttons.push(btn('upload', '选择本地 PDF'));
  }
  if (i.source !== 'demo' && i.doi) buttons.push(btn('metadata', '查询作者与摘要'));
  if (i.source === 'import' && i.doi && !i.pdf && i.stage === 'queued') buttons.push(btn('oa-fetch', '查找免费全文', pipe?.oa_running ? 'disabled' : ''));
  if (i.source === 'import' && i.doi && !i.pdf && i.stage === 'queued') buttons.push(btn('oa-lookup', '仅查找链接', pipe?.oa_running ? 'disabled' : ''));
  if (oa?.status === 'needs_human') {
    buttons.push(btn('oa-human', '打开验证窗口', pipe?.oa_running ? 'disabled' : ''), btn('oa-skip', '改走科研通'));
  }
  const page = [oa?.landing_url, oa?.manual_url].find(u => /^https?:\/\//.test(u || ''));
  if (page && !i.pdf) {
    buttons.push(`<a href="${esc(page)}" target="_blank" rel="noopener noreferrer">打开免费全文页面 ↗</a>`);
  }
  const links = !i.pdf && i.stage === 'queued' ? (oa?.links || []).filter(l => /^https?:\/\//.test(l.url || '')) : [];
  const linkList = links.length
    ? `<div class="verdict"><b>免费全文来源${oa.oa_status ? '（' + esc(oa.oa_status) + '）' : ''}</b><ul class="oalinks">${links.map((l, n) => `<li>${esc(l.source)} · <a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(urlHost(l.url))} ↗</a> <button data-action="oa-download" data-id="${id}|${n}" ${pipe?.oa_running ? 'disabled' : ''}>下载</button></li>`).join('')}</ul></div>` : '';
  if (i.source === 'import') buttons.push(btn('prepare', '填写求助表单', dis));
  if (i.source === 'import' && i.stage === 'queued') {
    buttons.push(btn('publish', '发布求助', gateLocked ? 'disabled title="需先处理已收到的应助，才能继续求助"' : dis));
  }
  if (i.source === 'website') {
    if (!i.pdf) buttons.push(gone ? btn('download', '下载并核对', 'disabled title="科研通文件已删除"') : btn('download', i.status === 'retrying' || i.download_failure ? '重试' : '下载并核对', dis));
    buttons.push(btn('review', '打开求助详情', dis));
  }
  if (i.stage === 'awaiting_decision' && i.source !== 'demo') buttons.push(btn('adopt', '采纳', `class="primary" ${dis}`));
  if (i.stage === 'oa_review') buttons.push(btn('oa-accept', '保留为完成', 'class="primary"'), btn('oa-discard', '改走科研通（保留文件）'));
  const delRow = dl.state && dl.state !== 'none'
    ? `<div class="deltag">${dl.state === 'deleted'
      ? `<span class="tag deleted">科研通文件已删除</span>${i.pdf ? '<span class="tag keep">本地副本保留</span>' : '<span>已无法下载</span>'}`
      : `<span>${esc(dl.text)}</span>${dl.at ? `<span class="tag">剩余 <span class="cd" data-at="${esc(dl.at)}"></span></span>` : ''}`}</div>` : '';
  return `<article class="task" data-task="${id}"><div class="tasktop"><div><div class="source">${esc(src)}</div><h3>${esc(i.title || '标题待补充')}</h3><div class="doi">${esc(i.expected_doi || i.doi || '未提供 DOI')}</div></div><div class="chips">${oaChip}${versionChip(i.oa_check)}${unconfirmed}${stageChip}${i.acquisition ? `<span class="tag">${esc(i.acquisition.label)}</span>` : ''}<span class="tag workflow-${esc(w)}"><span class="status-dot"></span>${esc(workflowNames[w] || w)}</span></div></div>${linkList}${delRow}${localFileRow(i)}${retractionWarning(i)}${downloadInfo(i)}${handoverInfo(i)}${publishInfo(i)}${i.note ? `<div class="note">${esc(human(i.note))}</div>` : ''}${v ? `<div class="verdict"><b>${esc(human(v.reason))}</b><div>标题相似度 ${esc(v.title_score)}%${v.pages ? ' · ' + v.pages + ' 页' : ''}</div></div>` : ''}${bookInfo(i)}${rejectionInfo(i)}${aiInfo(i)}<div class="actions">${buttons.join('')}${i.stage === 'attention' ? '<button data-action="settings" data-id="logs" title="生成已脱敏的错误报告">生成错误报告</button>' : ''}</div></article>`;
}

/* ---------- main render ---------- */
function renderPipelineStrip(busy) {
  const strip = $('pipelineStrip'), p = state.pipeline;
  if (!p || typeof p !== 'object') { strip.hidden = true; return; }
  strip.hidden = false;
  const g = p.gate || {};
  const gateItem = g.item_id ? findItem(g.item_id) : null;
  const chip = $('gateChip');
  if (g.blocked) {
    chip.className = 'badge rounded-pill gate locked';
    chip.textContent = gateItem ? `需先处理已收到的应助，才能继续求助（《${gateItem.title || '标题待补充'}》）` : '需先处理已收到的应助，才能继续求助';
  } else {
    chip.className = 'badge rounded-pill gate open';
    chip.textContent = '可继续求助';
  }
  const ns = p.next_step;
  $('nextLabel').textContent = ns ? (ns.label || ns.action || '—') : '暂无';
  $('nextReason').textContent = ns?.reason ? human(ns.reason) : '';
  $('pipeState').textContent = p.enabled ? '自动流程：运行中' : '自动流程：未开启';
  const idle = !ns || ['idle', 'wait'].includes(ns.action);
  $('stepBtn').disabled = busy || idle || !!ns.requires_user;
  $('stepBtn').title = ns?.requires_user ? '此步骤需手动处理' : '';
}

function hhmm(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
}

function renderMonitorChip() {
  const chip = $('monitorChip'), m = state.monitor;
  if (!m) { chip.hidden = true; return; }
  chip.hidden = false;
  const next = m.enabled && !m.login_required && m.next_sync_at ? ' · 下次同步 ' + hhmm(m.next_sync_at) : '';
  chip.textContent = '后台监控：' + m.label + next;
  chip.classList.toggle('off', !m.enabled);
  chip.classList.toggle('warn', !!m.login_required);
}

function render() {
  const items = state.items || [], busy = !!(state.busy || state.waiting);
  const prefs = state.preferences || {}, ai = state.ai || {}, account = state.account || {};

  const accountOptions = (state.accounts || []).map(a => `<option value="${esc(a.id)}">${esc(a.label)}${a.credentials_saved ? ' · 已保存凭据' : ''}</option>`).join('');
  if (accountOptions !== lastAccounts) { $('accountSelect').innerHTML = accountOptions; lastAccounts = accountOptions; }
  $('accountSelect').value = state.active_account_id || 'default';
  $('accountSelect').disabled = busy;

  const pts = state.pipeline?.points ?? account.points;
  $('pointsValue').textContent = pts ?? '待刷新';
  const tier = state.pipeline?.tier;
  $('tierLabel').textContent = tier ? (tier === 'batch' ? '积分 ≥ 500：可批量求助' : '积分 < 500：逐条求助') : '积分 ≥ 500 可批量求助';
  $('mode').textContent = ai.mode_label || '本地模式 · AI 关闭';
  $('mode').className = 'badge rounded-pill ' + (ai.enabled ? 'text-bg-success' : 'text-bg-secondary');
  const wk = state.download_worker, wc = $('workerChip');
  wc.hidden = !(wk && wk.enabled);
  if (!wc.hidden) { const busyW = wk.status === 'downloading'; wc.textContent = '下载线程：' + (busyW ? '下载中' : '空闲'); wc.className = 'badge rounded-pill ' + (busyW ? 'text-bg-primary' : 'text-bg-light border'); wc.title = busyW && wk.item_id ? '正在下载：' + (findItem(wk.item_id)?.title || '') : '后台下载线程状态'; }
  renderMonitorChip();

  const noSession = state.pipeline && state.pipeline.session === false;
  $('checkinStatus').textContent = noSession && !account.last_checkin_result ? '科研通未登录（可选）。免费全文无需登录。' : account.last_checkin_result ? `${account.last_checkin_result} · ${account.points ?? '待刷新'} 积分` : '暂无签到记录';
  $('browserState').textContent = busy ? '正在处理，请稍候…' : '当前无进行中的操作';
  document.querySelectorAll('[data-busy]').forEach(b => { b.disabled = busy; });

  renderPipelineStrip(busy);

  $('count-all').textContent = items.length;
  for (const name of ['queued', 'downloading', 'review', 'downloaded', 'done']) {
    $('count-' + name).textContent = items.filter(i => i.workflow === name).length;
  }
  document.querySelectorAll('.filter').forEach(b => {
    const on = b.dataset.filter === filter;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  });
  $('queueSummary').textContent = `共 ${items.length} 条（含演示样例）`;

  const selected = items.filter(i => filter === 'all' || i.workflow === filter);
  const html = selected.length
    ? selected.map(i => taskHtml(i, busy)).join('')
    : '<div class="empty"><b>暂无任务</b>粘贴文献或同步求助后，任务将显示在此处。</div>';
  if (html !== lastTasks) { $('tasks').innerHTML = html; bsify($('tasks')); lastTasks = html; }

  $('declare').hidden = !!prefs.home_notice_hidden;
  syncAiButtons();
  maybeOpenDisclaimer();
  const rl = $('repoLink'), repo = state.about?.repo_url;
  rl.innerHTML = repo ? `<a href="${esc(repo)}" target="_blank" rel="noopener noreferrer">GitHub ↗</a>` : '仓库地址未设置';
  renderPanel();
  maybeOpenIntake();
  tickCountdowns();

  if (settingsOpen()) renderSettingsPanel();
}

async function refresh() {
  try {
    const response = await fetch('/api/state');
    state = await response.json();
    render();
  } catch (e) {
    $('browserState').textContent = '后台服务未连接，请重新启动。';
  }
}


/* ---------- reward tiers (official tiers only; tiers above the current points are disabled) ---------- */
const REWARD_STEPS_DEFAULT = [10, 20, 30, 50, 80, 100, 150, 200];
function rewardAnchors(key) {
  if (key === 'raise_max') return (state.publish?.raise_steps || [10, 20, 30, 50, 80, 100, 150, 200, 300, 500]).slice();
  return (state.reward?.steps || REWARD_STEPS_DEFAULT).slice();
}
const rewardCap = key => key === 'raise_max' ? Infinity : (state.reward?.cap ?? 200);
function rewardIndex(value, a, cap) {
  let i = a.findIndex(t => t >= value);
  if (i < 0) i = a.length - 1;
  if (a[i] > value && i > 0) i -= 1;      // a stored value between tiers shows as the tier below it
  while (i > 0 && a[i] > cap) i -= 1;
  return i;
}
function rewardControl(key, value) {
  const a = rewardAnchors(key), cap = rewardCap(key);
  if (!a.length || a[0] > cap) return '<span class="hint">当前积分不足 10，暂时无法设置悬赏。</span>';
  const v = a[rewardIndex(value || state.reward?.default || 10, a, cap)], k = esc(key);
  return `<div class="btn-group btn-group-sm rw-group" role="radiogroup" aria-label="悬赏积分">${a.map(t => `<input type="radio" class="btn-check rw-opt" name="rw-${k}" id="rw-${k}-${t}" value="${t}" data-rw="${k}" autocomplete="off" ${t === v ? 'checked' : ''} ${t > cap ? 'disabled' : ''}><label class="btn btn-outline-primary" for="rw-${k}-${t}" ${t > cap ? 'title="超过当前积分"' : ''}>${t}</label>`).join('')}</div>`;
}
function rewardRow() {
  const rw = state.reward;
  if (!rw) return '';
  return `<div class="setrow"><div><b>默认悬赏</b><small>发起求助时使用的悬赏积分，只能选择科研通官方档位（${rewardAnchors().join(' / ')}），且不能超过当前积分（${esc(rw.cap)}）。文献要求的最低悬赏更高时，按“最低悬赏上调”的设置处理。每条求助可在确认清单中单独调整。当前默认：<b>${esc(rw.default)}</b> 积分。</small></div>${rewardControl('default', rw.default)}</div>`;
}
document.addEventListener('change', e => {
  const t = e.target;
  if (!t.classList?.contains('rw-opt')) return;
  const key = t.dataset.rw, v = Number(t.value);
  if (key === 'default') action('preferences', { default_reward: v });
  else if (key === 'raise_max') action('pipeline', { raise_max: v });
  else if (intakeCtx) { intakeCtx.rewards[key] = v; updateIntakeSummary(); }
});

/* ---------- AI-only controls, first-run dialog ---------- */
function syncAiButtons() {
  const b = $('aiOrganizeBtn');
  if (!b) return;
  const on = aiOn(), has = importUnparsed.length > 0;
  b.disabled = !on || !has;
  b.title = !on ? AI_OFF_HINT : (has ? '' : '没有本地未识别的内容');
  $('aiOrganizeHint').textContent = !on ? AI_OFF_HINT : (has ? `有 ${importUnparsed.length} 段内容未能在本地识别，可使用 AI 整理（先预览，确认后导入）。` : '');
}

const DISCLAIMER_SHORT = [
  '科研通下载器为第三方个人开源工具，免费开源（AGPL-3.0），仅供学习交流使用；并非科研通（ablesci.com）官方程序，与科研通及其运营方无任何隶属、合作或授权关系。如您是付费获得本软件，则属受骗。',
  '获取文献时，本软件先查询合法的免费开放获取（OA）来源，能直接下载的即直接下载；查询不到时，才使用您本人的科研通账户在科研通网站发布求助，并通过科研通提供的下载渠道获取应助文件。所有科研通操作均以您本人账户进行，须遵守科研通规则，积分消耗由您自行承担。',
  '仅限个人学习与科研使用。请遵守著作权法、出版商许可及科研通规则；免费全文的许可各不相同，再利用前请查看具体许可。本软件不使用 Sci-Hub 等影子图书馆。',
  '风险提示：自动同步、发布、采纳、驳回可能因科研通规则变化而出错，并可能导致扣分、功能限制或封号。建议保持各项确认开启。因使用本软件造成的账号、积分或数据损失，作者不承担责任。',
  'AI 辅助默认关闭。开启后，每次发送内容前均需预览并同意；输出可能有误，产生的费用由用户自行承担。',
  '隐私：数据在本机处理，不增加积分，不上传个人信息。联网范围：科研通、公开书目与开放获取服务（发送 DOI 或检索词）以及用户开启的 AI 服务。数据保存在本机 data 目录。'
];

function openDisclaimer() {
  setModal('disclaimer', `${modalHead('使用前请阅读', '', 'id="disclaimerTitle"')}<div class="modal-body dialogbody"><ul class="disclist">${DISCLAIMER_SHORT.map(t => `<li>${esc(t)}</li>`).join('')}</ul><div class="actions"><button class="primary" data-action="ack-disclaimer">我已了解</button><button data-action="show-full-disclaimer">查看完整声明</button></div></div>`);
  mShow('disclaimer');
}

function maybeOpenDisclaimer() {
  if (disclaimerShown || !state.about?.disclaimer_pending) return;
  if (settingsOpen() || mOpen('dialog') || mOpen('intake')) return;
  disclaimerShown = true;
  openDisclaimer();
}

const DISCLAIMER_FULL = [
  ['1. 软件性质：第三方、免费、开源', ['科研通下载器为第三方个人开源工具，并非科研通（ablesci.com）官方程序，与科研通及其运营方无任何隶属、合作或授权关系。',
    '本软件免费开源（GNU AGPL-3.0），仅供学习交流使用。如您是付费购买的本软件，则属受骗。未经作者许可，不得将本软件打包后收费销售或用于其他商业目的。']],
  ['2. 获取方式与用途限制', ['获取文献时，本软件先查询合法的免费开放获取（OA）来源（如 OpenAlex、Europe PMC、PubMed Central、Unpaywall、arXiv 等），能直接下载的即直接下载。',
    '仅当上述来源查询不到时，本软件才使用用户本人的科研通账户，在科研通网站发布求助，并通过科研通提供的下载渠道下载应助文件。所有科研通操作均以用户本人账户进行，须遵守科研通规则，积分消耗由用户自行承担。',
    '本软件不使用 Sci-Hub 等影子图书馆，尊重版权。本软件仅供个人学习与科研使用，禁止用于牟利、代他人求助、转售文献或任何违法目的。']],
  ['3. 用户的合规责任', ['用户应自行遵守：所在地著作权法及相关法律法规；出版商及数据库的许可条款；所在机构（学校、单位、图书馆）的规定；科研通（ablesci.com）的全部规则，包括但不限于：',
    '· 仅使用本人注册的账号，不代他人求助，不转移积分，不使用多个账号；',
    '· 科研通上的文件仅供个人学习，不得修改、再分发或牟利；本软件保存文件时保留原始文件名，不修改原件；',
    '· 求助完成约 24 小时后，科研通会自动删除文件，请及时处理；已下载的本地副本由用户自行负责；',
    '· 驳回只能依据科研通允许的理由；滥用驳回、虚假理由、扰乱求助秩序可能被扣分或封号；',
    '· 规则以科研通页面的实时内容为准，可能随时变化。本软件文档中的规则摘要仅反映某一时间点，不构成官方说明。']],
  ['4. 与第三方无关联', ['本软件为独立的第三方个人工具，与科研通、OpenAlex、Europe PMC、PubMed Central、Semantic Scholar、Unpaywall、arXiv、bioRxiv、Crossref、DeepSeek 及任何出版商均无隶属、合作或授权关系，也未获其认可。相关名称与商标归各自权利人所有，仅用于说明兼容性或数据来源。']],
  ['5. 免费全文的使用', ['免费全文（开放获取）的许可各不相同，例如 CC BY 允许署名后再利用，其他许可可能限制商用、改编或再分发。下载前后请自行查看该文献的许可。本软件显示的开放获取状态来自第三方数据，可能不完整或有误。']],
  ['6. 自动化风险', ['本软件提供同步、定时查看、签到、自动发布、自动下载、自动采纳与驳回等自动功能。科研通的规则、页面结构、限制和风控可能随时变化，自动功能可能因此失效、误判或触发限制。是否启用自动功能由用户自行决定。建议保持各类确认步骤开启，尤其是自动驳回和自动采纳；这些操作可能导致扣分、封号或无法撤销的结果。本软件不保证任何自动流程可在无人值守时正确运行。']],
  ['7. 无担保与责任限制', ['本软件按“现状”提供，不作任何明示或默示担保，包括可用性、准确性、适销性或特定用途适用性。在法律允许的最大范围内，作者及贡献者不对因使用或无法使用本软件造成的任何损失负责，包括但不限于：账号被限制或封禁、积分被扣除或损失、文件被误判或被错误驳回或采纳、下载失败或重复扣分、数据丢失、与第三方的纠纷。本软件的 PDF 核对结果仅为本地启发式判断，不能替代人工确认。']],
  ['8. AI 功能', ['AI 功能默认关闭，需用户自行提供接口与密钥（DeepSeek 或兼容 OpenAI 接口的自定义服务）。AI 输出可能有误，仅作人工辅助，不参与自动驳回、采纳或发布的判断。使用 AI 产生的费用、额度、内容安全与合规问题，由用户与服务商自行处理。开启后向所配置的服务发送的内容，受该服务商条款与隐私政策约束；开启云端 AI 后，软件不再属于完全离线。']],
  ['9. 隐私与数据', ['· 数据在本机处理，不增加积分，不上传个人信息。', '· 默认的解析与 PDF 核对在本机完成。',
    '· 联网情形：登录、同步、签到、下载时访问科研通；公开书目查询时向 OpenAlex、Crossref、Europe PMC、Semantic Scholar、Unpaywall、arXiv、bioRxiv 等发送 DOI 或检索词；开启 AI 并经用户逐次预览同意后，向所配置的 AI 服务发送选定的页图或文本片段。',
    '· 不会向 AI 发送整本 PDF、本地路径、Cookie、凭据或任务队列。',
    '· 账户会话、文件和记录保存在本机 data 目录，请勿公开分享。密码可选择使用 Windows DPAPI 加密保存，密文绑定当前 Windows 用户；AI 密钥可选择仅保存在内存，或使用 DPAPI 保存。',
    '· 通过 MCP 或命令行连接的外部智能体如何处理数据，取决于该客户端，本软件无法控制。']],
  ['10. 开源许可证', ['本软件以 GNU AGPL-3.0 发布（见 LICENSE）。核心依赖 PyMuPDF 采用 AGPL-3.0，因此选择该许可证；第三方依赖的许可见 THIRD_PARTY_NOTICES.md。']],
  ['11. 反馈与联系', ['问题与建议请在仓库提交 Issue（见下方仓库地址）。版权方或科研通如认为本软件的某项功能不当，请通过上述方式联系，作者将及时评估并处理。提交问题时请去除 Cookie、密码、密钥和个人信息。']],
  ['12. 声明的变更', ['本声明可能随版本更新而修改，以最新版本为准。作者保留在发现违规用途时停止维护或分发的权利。']]
];

function tabLogs() {
  const L = logsState, ab = state.about || {};
  const errs = L.errors === null ? '<p class="hint">正在读取…</p>' : (L.errors.length
    ? `<ul class="errlist">${L.errors.map(e => `<li><label class="check"><input type="checkbox" class="lg-err" data-id="${esc(e.id)}" ${L.selected.has(e.id) ? 'checked' : ''}> <time>${esc(String(e.time).slice(5, 16).replace('T', ' '))}</time> ${esc(e.module)} / ${esc(e.action)} · ${esc(e.error_type || '')} ${esc(e.message || '')}</label></li>`).join('')}</ul>`
    : '<p class="hint">最近没有错误记录。</p>');
  const a = L.analysis;
  const analysis = a ? `<div class="verdict"><b>AI 分析（仅供参考，软件不会据此执行任何操作）</b><p>可能原因：</p><ul>${a.causes.map(t => `<li>${esc(t)}</li>`).join('') || '<li>—</li>'}</ul><p>建议：</p><ul>${a.suggestions.map(t => `<li>${esc(t)}</li>`).join('') || '<li>—</li>'}</ul></div>` : '';
  const ai = aiOn();
  return `<h4>日志反馈</h4>
<p>运行错误记录在本机 data/logs 目录，保留 14 天，仅清理软件自身的日志文件。写入前会去除邮箱、密码、密钥、Cookie、链接参数和用户名。可选择错误生成报告，预览后再决定是否保存或反馈。</p>
<div class="actions"><button data-action="logs-load">刷新错误列表</button></div>
<h4>最近错误</h4>${errs}
<div class="actions"><button class="primary" data-action="report-build">生成报告预览</button></div>
${L.report !== null ? `<h4>报告预览（已脱敏）</h4><textarea class="reportbox" readonly rows="14" aria-label="报告预览">${esc(L.report)}</textarea>
<p class="hint">提交前请再次检查报告中不含个人信息。</p>
<label class="check"><input id="reportConsent" type="checkbox" ${L.consent ? 'checked' : ''} ${ai ? '' : 'disabled'}>同意将上方预览的报告发送至已配置的 AI 服务进行分析</label>
<div class="actions"><button data-action="report-ai" ${ai ? '' : 'disabled'} title="${ai ? '' : AI_OFF_HINT}">AI 分析</button>${ai ? '' : `<span class="hint">${AI_OFF_HINT}</span>`}<button data-action="report-save">保存报告（.md）</button><button data-action="report-issue" ${ab.issue_enabled ? '' : 'disabled'}>打开 GitHub Issue</button>${ab.issue_enabled ? '' : '<span class="hint">仓库地址未设置</span>'}</div>
<p class="hint">GitHub Issue 将在新标签页打开预填的简短说明，请附上保存的报告文件。软件不会自动提交，也不需要 GitHub 登录信息。</p>${analysis}` : ''}`;
}

function downloadText(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function loadLogs() {
  try {
    logsState.errors = (await post('logs/recent')).errors || [];
    lastPanel = ''; if (settingsOpen() && settingsTab === 'logs') renderSettingsPanel();
  } catch (e) { notice(e.message); }
}

function aiFormBody(enabled, consent = false) {
  const provider = $('aiProvider').value;
  const body = { enabled, provider, api_key: $('aiKey').value, storage: document.querySelector('#aiStorage input:checked')?.value || 'memory', consent };
  if (provider === 'custom') { body.base_url = $('aiBase').value.trim(); body.model = $('aiModelName').value.trim(); }
  return body;
}

document.addEventListener('change', e => {
  const t = e.target;
  if (t.id === 'aiProvider') $('aiCustom').hidden = t.value !== 'custom';
  if (t.classList?.contains('lg-err')) { t.checked ? logsState.selected.add(t.dataset.id) : logsState.selected.delete(t.dataset.id); }
  if (t.id === 'reportConsent') logsState.consent = t.checked;
});

function tabAbout() {
  const hidden = !!state.preferences?.home_notice_hidden, ab = state.about || {};
  const full = DISCLAIMER_FULL.map(([h, ps]) => `<h5>${esc(h)}</h5>${ps.map(p => `<p>${esc(p)}</p>`).join('')}`).join('');
  return `<h4>关于</h4>
<div class="aboutbox"><p><b>科研通下载器为第三方个人开源工具，免费开源，仅供学习交流使用；并非科研通官方程序，与科研通无隶属、合作或授权关系。如您是付费获得，则属受骗。</b></p>
<p>支持科研通：<a href="https://www.ablesci.com/my/point-donate" target="_blank" rel="noopener noreferrer">捐助积分 ↗</a>　<a href="https://www.ablesci.com/post/detail?id=P7j5Qn" target="_blank" rel="noopener noreferrer">无偿捐助 ↗</a>（均为科研通官方入口）</p></div>
<p>版本：<b>${esc(ab.version || '未知')}</b>　声明版本：${esc(ab.disclaimer_version || '')}　许可证：GNU AGPL-3.0</p>
<p>仓库地址：${ab.repo_url ? `<a href="${esc(ab.repo_url)}" target="_blank" rel="noopener noreferrer">${esc(ab.repo_url)} ↗</a>` : '未设置'}</p>
<div class="setrow"><div><b>首页支持提示</b><small>${hidden ? '当前已隐藏。' : '当前显示在首页顶部，可点击提示右侧的“关闭”隐藏。'}</small></div><button data-action="show-home-notice" ${hidden ? '' : 'disabled'}>重新显示</button></div>
<div class="setrow"><div><b>首次使用提示</b><small>可随时重新阅读。</small></div><button data-action="show-first-run">查看</button></div>
<h4>免责声明与使用须知</h4>
<p class="hint">使用本软件即表示已阅读并理解以下内容；如不同意，请停止使用。</p>
<div class="disclaimer-full">${full}</div>`;
}

/* ---------- message panel ---------- */
let lastPanelSig = '', intakeCtx = null, intakeShown = new Set();
const stMark = { done: '✓', active: '…', waiting_you: '●', error: '!', pending: '○', skipped: '–' };

function needMe() {
  const out = [];
  if (state.pipeline?.restart_notice) out.push({ text: '自动流程已在重启后关闭', action: 'pipeline-enable', id: 'enabled', label: '重新开启' });
  for (const b of state.batches || []) if (b.phase === 'awaiting_confirmation') out.push({ text: `本次导入的 ${b.total} 条文献已筛选完成，待确认是否求助`, action: 'open-batch', id: b.id, label: '打开确认清单' });
  for (const i of state.items || []) {
    const t = '《' + (i.title || '标题待补充').slice(0, 30) + '》';
    if (i.stage === 'awaiting_decision') out.push({ text: t + ' 已下载，待采纳或驳回', action: 'adopt', id: i.id, label: '去处理' });
    else if (i.stage === 'attention') out.push({ text: t + ' 需处理', action: 'focus', id: i.id, label: '查看' });
    else if (i.stage === 'oa_review') out.push({ text: t + ' 免费全文待确认', action: 'focus', id: i.id, label: '查看' });
    else if (i.oa_check?.status === 'needs_human') out.push({ text: t + ' 免费全文页面需人工验证', action: 'oa-human', id: i.id, label: '打开验证窗口' });
    const d = i.deletion;
    if (d?.state === 'scheduled' && d.at && !i.pdf && Date.parse(d.at) - Date.now() <= 7200000) out.push({ text: t + ' 科研通文件即将删除，尚未下载', action: 'focus', id: i.id, label: '去下载' });
  }
  return out;
}

function eventButton(e) {
  if (!e.action) return '';
  if (e.action === 'open-batch' && e.batch_id) return `<button data-action="open-batch" data-id="${esc(e.batch_id)}">打开确认清单</button>`;
  if (e.action === 'login') return '<button data-action="login">登录</button>';
  if (e.action === 'pipeline-enable') return state.pipeline?.restart_notice ? '<button data-action="pipeline-enable" data-id="enabled">重新开启</button>' : '';
  const item = e.item_id ? findItem(e.item_id) : null;
  if (!item) return '';
  if (e.action === 'oa-human' && item.oa_check?.status === 'needs_human') return `<button data-action="oa-human" data-id="${esc(item.id)}">打开验证窗口</button>`;
  return `<button data-action="focus" data-id="${esc(item.id)}">查看</button>`;
}

function renderPanel() {
  const batches = state.batches || [], need = needMe();
  const running = batches.filter(b => ['screening', 'requesting', 'monitoring'].includes(b.phase)).length;
  const hot = need.length;
  const cards = batches.slice(0, 4).map(b => `<div class="batchcard"><header><b>${esc(b.created.slice(5, 16).replace('T', ' '))} 导入</b><span>${esc({ screening: '筛选中', awaiting_confirmation: '待确认', requesting: '求助中', monitoring: '等待应助', done: '已结束' }[b.phase] || '')}</span></header><ul class="timeline">${b.steps.map(st => `<li><span class="mark st-${esc(st.status)}">${stMark[st.status] || '○'}</span><span class="st-${esc(st.status)}">${esc(st.label)}${st.detail ? ` <small>${esc(st.detail)}</small>` : ''}</span></li>`).join('')}</ul>${b.phase === 'awaiting_confirmation' ? `<div class="actions"><button class="primary" data-action="open-batch" data-id="${esc(b.id)}">打开确认清单</button></div>` : ''}</div>`).join('');
  const evs = (state.events || []).slice(-50).reverse().map(e => `<li><time>${esc(String(e.time).slice(11, 16))}</time><span>${esc(human(e.message))}</span>${eventButton(e)}</li>`).join('');
  const body = `<div class="msgbody">${need.length ? `<h4>需处理（${need.length}）</h4>${need.slice(0, 8).map(n => `<div class="needme"><span>${esc(n.text)}</span><button data-action="${esc(n.action)}" data-id="${esc(n.id)}">${esc(n.label)}</button></div>`).join('')}` : ''}${cards ? `<h4>导入进度</h4>${cards}` : ''}<h4>最近动态</h4><ul class="evlist">${evs || '<li>暂无动态</li>'}</ul></div>`;
  const sig = [running, hot, cards, evs, need.map(n => n.text).join('|')].join('#');
  if (sig === lastPanelSig) return;
  lastPanelSig = sig;
  const holder = $('msgBody'), scroll = holder.scrollTop || 0;
  holder.innerHTML = body;
  bsify(holder);
  holder.scrollTop = scroll;
  $('msgBadges').innerHTML = `${running ? `<span class="badge text-bg-light">进行中 ${running}</span> ` : ''}${hot ? `<span class="badge text-bg-danger">需处理 ${hot}</span>` : ''}`;
}

/* ---------- confirmation list ---------- */
function maybeOpenIntake() {
  if (mOpen('intake') || settingsOpen() || mOpen('dialog') || mOpen('disclaimer')) return;
  const b = (state.batches || []).find(x => x.phase === 'awaiting_confirmation' && !x.prompted && !intakeShown.has(x.id));
  if (!b) return;
  intakeShown.add(b.id);
  post('batch/seen', { batch_id: b.id }).catch(() => {});
  openIntake(b.id);
}

function openIntake(batchId) {
  const b = (state.batches || []).find(x => x.id === batchId);
  if (!b) return notice('未找到该导入记录');
  intakeCtx = { batchId, checked: new Set(b.items.filter(r => r.checked).map(r => r.id)), rewards: Object.fromEntries(b.items.map(r => [r.id, r.reward])) };
  const rows = b.items.map(r => `<tr class="${r.selectable ? '' : 'off'}"><td><input type="checkbox" class="ik" data-id="${esc(r.id)}" ${r.checked ? 'checked' : ''} ${r.selectable ? '' : 'disabled'} aria-label="选择该文献"></td><td class="t">${esc(r.title || '标题待补充')}</td><td class="d">${esc(r.doi || '—')}</td><td>${esc(r.result)}${r.version ? `<br><small>版本：${esc(versionNames[r.version] || r.version)}</small>` : ''}${r.retracted ? '<br><b class="danger">已撤稿或有更正通知，请先核实</b>' : ''}</td><td>${esc(r.advice)}</td><td class="rw">${r.selectable ? rewardControl(r.id, r.reward) : '—'}</td></tr>`).join('');
  setModal('intake', `${modalHead('确认求助清单', 'intake-close', 'id="intakeTitle"')}<div class="modal-body dialogbody"><div class="intakesum" id="intakeSum"></div><div class="tablewrap"><table class="table table-sm table-hover align-middle mb-0 intaketable"><thead><tr><th>选择</th><th>标题</th><th>DOI</th><th>免费全文结果</th><th>建议</th><th>悬赏</th></tr></thead><tbody>${rows}</tbody></table></div><p class="hint">默认勾选“无免费全文”或“免费全文下载失败”且有 DOI 的文献。确认后，仅已勾选的文献会向科研通发布求助（扣除积分），未勾选的不会发布。</p><div class="intakefoot" id="intakeFoot"></div></div>`);
  mShow('intake');
  updateIntakeSummary();
}

function updateIntakeSummary() {
  const c = intakeCtx, el = $('intakeSum');
  if (!c || !el) return;
  const b = (state.batches || []).find(x => x.id === c.batchId);
  if (!b) return;
  const p = state.pipeline || {}, pts = p.points, noSession = p.session === false;
  const chosen = [...c.checked];
  const total = chosen.reduce((n, id) => n + (c.rewards[id] || 0), 0);
  const gate = p.gate?.blocked ? '<span class="gatebox">注意：需先处理已收到的应助，才能继续求助。目前仍有未处理的应助，新的求助将排队等待。</span>' : '当前可直接开始求助。';
  const tierText = p.tier === 'batch' ? '积分达到 500，可连续求助。' : '积分低于 500：一次只能求助一条，其余排队；有人应助后，需先采纳或驳回才能继续求助。';
  el.innerHTML = `<div>当前积分：<b>${esc(pts ?? '未知')}</b>　已选：<b>${chosen.length}</b> 条　悬赏合计：<b>${total}</b> 积分${typeof pts === 'number' && total > pts ? ' <b class="danger">（超出当前积分）</b>' : ''}</div><div class="hint" style="margin:0">实际以科研通要求的最低悬赏为准，可能更高。</div><div>${tierText}</div><div>${gate}</div>`;
  const foot = $('intakeFoot');
  const sig = [noSession, chosen.length].join('|');
  if (foot.dataset.sig !== sig) {
    foot.dataset.sig = sig;
    foot.innerHTML = `${noSession ? '<button class="primary" data-action="login">登录科研通</button>' : `<button class="primary" data-action="intake-confirm" ${chosen.length ? '' : 'disabled'}>确认并开始求助</button>`}<button data-action="intake-later">稍后确认</button><button data-action="intake-csv">导出 CSV</button>${noSession ? '<span class="hint">未检测到科研通登录。免费全文筛选结果已保存，登录后可随时回来确认。</span>' : ''}`;
    bsify(foot);
  }
}

document.addEventListener('change', e => {
  if (e.target.classList?.contains('ik') && intakeCtx) {
    e.target.checked ? intakeCtx.checked.add(e.target.dataset.id) : intakeCtx.checked.delete(e.target.dataset.id);
    updateIntakeSummary();
  }
});

function intakeCsv() {
  const b = (state.batches || []).find(x => x.id === intakeCtx?.batchId);
  if (!b) return;
  const cell = v => { v = String(v ?? ''); if (/^[=+\-@\t\r]/.test(v)) v = "'" + v; return '"' + v.replaceAll('"', '""') + '"'; };
  const lines = [['选择', '标题', 'DOI', '免费全文结果', '建议', '悬赏'].map(cell).join(',')].concat(b.items.map(r => [intakeCtx.checked.has(r.id) ? '是' : '否', r.title, r.doi, r.result, r.advice, intakeCtx.rewards[r.id] ?? r.reward].map(cell).join(',')));
  const url = URL.createObjectURL(new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url; a.download = 'intake-' + b.id + '.csv';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function focusItem(id) {
  filter = 'all'; lastTasks = ''; render();
  mOpen('settings') && mHide('settings');
  const el = document.querySelector(`[data-task="${CSS.escape(id)}"]`);
  if (el) { el.scrollIntoView({ block: 'center' }); el.style.outline = '3px solid var(--focus)'; setTimeout(() => { el.style.outline = ''; }, 2500); }
}

/* ---------- deletion countdown ---------- */
function tickCountdowns() {
  document.querySelectorAll('.cd').forEach(el => {
    const left = Date.parse(el.dataset.at) - Date.now();
    if (!(left > 0)) { el.textContent = '已到期，正在确认'; return; }
    const h = Math.floor(left / 3600000), m = Math.floor(left % 3600000 / 60000), s = Math.floor(left % 60000 / 1000);
    el.textContent = `${h} 小时 ${m} 分 ${s} 秒`;
  });
}
setInterval(tickCountdowns, 1000);

/* ---------- settings dialog ---------- */
function openSettings(tab) {
  if (tab && SETTINGS_TABS.some(t => t[0] === tab)) settingsTab = tab;
  const tabs = SETTINGS_TABS.map(([k, n]) => `<button role="tab" class="nav-link text-start" id="tab-${k}" data-action="tab" data-id="${k}" aria-controls="tabpanel">${n}</button>`).join('');
  setModal('settings', `${modalHead('设置', 'close-settings', 'id="settingsTitle"')}<div class="modal-body p-0"><div class="settings-body"><div class="tabs nav nav-pills flex-column" role="tablist" aria-label="设置分区" aria-orientation="vertical">${tabs}</div><div class="tabpanel" id="tabpanel" role="tabpanel" tabindex="0"></div></div></div>`);
  lastPanel = '';
  if (!settingsOpen()) mShow('settings');
  renderSettingsPanel();
  if (settingsTab === 'mcp') loadMCP();
  if (settingsTab === 'logs') loadLogs();
}

function renderSettingsPanel() {
  const panel = $('tabpanel');
  if (!panel) return;
  document.querySelectorAll('.tabs [role=tab]').forEach(b => {
    const on = b.dataset.id === settingsTab;
    b.setAttribute('aria-selected', String(on));
    b.classList.toggle('active', on);
    b.tabIndex = on ? 0 : -1;
  });
  panel.setAttribute('aria-labelledby', 'tab-' + settingsTab);
  const html = ({
    privacy: tabPrivacy, donate: tabDonate, ai: tabAI, mcp: tabMCP, download: tabDownload,
    reject: tabReject, pipeline: tabPipeline, notify: tabNotify, accounts: tabAccounts, logs: tabLogs, about: tabAbout
  })[settingsTab]();
  const sig = settingsTab + '|' + html;
  if (sig === lastPanel) return;  // unchanged: keep typed input intact
  lastPanel = sig;
  panel.innerHTML = html;
  bsify(panel);
}

/* numeric field with a unit suffix */
const numField = (id, value, rg, label, unit) => `<div class="input-group input-group-sm numfield"><input id="${id}" class="form-control" type="number" min="${esc(rg[0])}" max="${esc(rg[1])}" step="1" value="${esc(value)}" aria-label="${esc(label)}"><span class="input-group-text">${unit}</span></div>`;
const rangeText = (rg, unit, def) => `范围 ${esc((rg || [])[0])}–${esc((rg || [])[1])} ${unit}，默认 ${esc(def)}。`;

function tabPrivacy() {
  const ai = state.ai?.enabled;
  return `<h4>隐私与联网</h4>
<p>本软件不生成或赠送科研通积分；签到及捐助积分由科研通按规则发放。</p>
<p>${ai ? 'AI 辅助已开启：每次发送前均需预览并确认，才会将所选页图或文字发送至已配置的 AI 服务，其余步骤均在本机处理。' : 'AI 辅助未开启：使用本地固定规则处理，不调用 AI，不上传 PDF 或个人信息。'}</p>
<p>联网范围：登录、签到和下载需连接科研通；公开书目查询仅向 OpenAlex、Crossref 发送 DOI（对方可见来源 IP）。密码可使用 Windows 用户加密保存；登录状态、PDF 和记录均保存在本机。启用 AI 并逐次确认后，才会向已配置的 AI 服务发送所选页图或文字片段。通过 MCP 连接的外部工具可读取工具返回的摘要，并可能按其自身设置将内容交给云端模型。</p>`;
}

function tabDonate() {
  return `<h4>捐助</h4>
<p>自愿支持科研通：</p>
<ul><li><a href="https://www.ablesci.com/my/point-donate" target="_blank" rel="noopener noreferrer">捐助获取积分 ↗</a></li><li><a href="https://www.ablesci.com/post/detail?id=P7j5Qn" target="_blank" rel="noopener noreferrer">无偿捐助 ↗</a></li></ul>
<p>以上均为科研通官方入口，本软件不收款。积分规则以捐助页面为准；无偿捐助不获得积分。</p>`;
}

function tabAI() {
  const a = state.ai || {}, on = a.enabled, custom = a.provider === 'custom';
  const presets = (a.presets || []).map(p => `<option value="${esc(p.id)}" ${a.provider === p.id ? 'selected' : ''}>${esc(p.label)}</option>`).join('');
  const dp = a.dpapi_available;
  return `<h4>AI 辅助</h4>
<div class="setrow"><div><b>启用 AI 辅助</b><small>${on ? '已开启：' + esc(a.provider_label) + ' / ' + esc(a.model) + '。' : '已关闭，不影响核心功能。'}AI 仅用于识别文字和图片，不做任何决定：不会自动驳回、采纳或发布，也不判断费用。每次发送前都会展示发送内容，经确认后才会发送。</small></div>${sw('ai-toggle', null, !!on, '启用 AI 辅助')}</div>
<table class="plain aitable"><thead><tr><th>功能</th><th>关闭时</th><th>开启后新增</th></tr></thead><tbody>
<tr><td>智能粘贴与文件导入</td><td>本地规则识别 DOI、标题、作者及 RIS、BibTeX、NBIB</td><td>本地未识别的内容可用“AI 整理”处理（先预览，确认后才导入）</td></tr>
<tr><td>PDF 核对</td><td>本地比对 DOI、标题、作者、摘要</td><td>扫描件或“未核实”的文件可用“AI 识别”处理 1–3 页页图；结果单独显示，需人工复核，不改变本地判定</td></tr>
<tr><td>免费全文下载、科研通同步 / 下载 / 求助 / 采纳 / 驳回、自动流程</td><td>全部可用</td><td>无变化，AI 不参与</td></tr>
<tr><td>错误报告</td><td>可生成已脱敏的报告并保存、反馈</td><td>可使用“AI 分析”（仅提供建议）</td></tr></tbody></table>
<div class="callout">注意：通过 Claude Code、Codex 等外部工具使用时，无需在此填写 AI 密钥。</div>
<label>接口<select id="aiProvider">${presets}<option value="custom" ${custom ? 'selected' : ''}>自定义</option></select></label>
<div id="aiCustom" ${custom ? '' : 'hidden'}>
<label>接口地址<input id="aiBase" autocomplete="off" maxlength="300" value="${esc(custom ? a.base_url : '')}" placeholder="https://api.example.com/v1"></label>
<label>模型名<input id="aiModelName" autocomplete="off" maxlength="100" value="${esc(custom ? a.model : '')}" placeholder="例如 gpt-4o-mini"></label>
<p class="hint">需兼容 OpenAI chat/completions，并支持图片输入（用于页图识别）。地址须为 https://；仅本机地址（localhost、127.0.0.1、::1）可使用 http://。</p></div>
<label>API Key<input id="aiKey" type="password" autocomplete="off" placeholder="${a.key_configured ? '已保存，重新填写将替换' : '粘贴密钥'}"></label>
<p class="hint">${a.key_configured ? esc(a.key_hint) + '（完整密钥不会再显示）' : '尚未保存密钥'}。密钥仅发送至上方配置的地址，不写入日志，也不提供给外部工具。</p>
<div class="setrow"><div><b>密钥保存方式</b><small>加密保存使用 Windows DPAPI，绑定当前 Windows 用户。</small></div>${segLocal('aiStorage', '密钥保存方式', [['dpapi', '加密保存'], ['memory', '仅本次运行']], a.storage === 'dpapi' ? 'dpapi' : 'memory', dp ? [] : ['dpapi'])}</div>
<div class="actions"><button data-action="ai-test">测试连接</button>${on ? '<button data-action="ai-save" class="primary">保存设置</button>' : ''}<button data-action="ai-forget" class="danger" ${a.key_configured ? '' : 'disabled'}>删除已保存的密钥</button></div>
<div id="aiTestResult" class="hint" role="status">“测试连接”仅查询模型列表，不发送文献内容。</div>`;
}

function tabMCP() {
  if (!mcpConfig) return '<h4>MCP 连接</h4><p class="hint">正在读取配置…</p>';
  const on = !!state.preferences?.mcp_website_actions;
  return `<h4>MCP 连接</h4>
<ol><li>保持本软件在后台运行。</li><li>Claude Code 使用项目中的 <b>.mcp.json</b>；Codex 使用 <b>.codex/config.toml</b>（需信任本项目）。其他客户端可复制下方配置。</li><li>在客户端中重新连接 MCP，并先调用 workbench_status 检查连接。</li><li>默认仅支持本地查询、导入和核对；需要代为操作科研通时，再开启下方权限。</li></ol>
<p class="hint">MCP 本身不调用 AI。外部工具可能按其自身设置，将返回的信息交给云端模型。</p>
<h4>Claude Code / 通用 JSON</h4><pre>${esc(JSON.stringify(mcpConfig.claude, null, 2))}</pre><button data-action="copy-mcp-json">复制 JSON</button>
<h4>Codex TOML</h4><pre>${esc(mcpConfig.codex)}</pre><button data-action="copy-mcp-toml">复制 TOML</button>
<div class="setrow"><div><b>允许 MCP 操作科研通</b><small>${on ? '已允许：外部工具可调用当前账户的同步、登录、签到、下载和驳回。' : '未允许：外部工具仅可使用本地功能。'}</small></div>${sw('toggle-mcp', null, on, '允许 MCP 操作科研通')}</div>`;
}

async function loadMCP() {
  try {
    mcpConfig = await (await fetch('/api/mcp-config')).json();
    if (settingsOpen() && settingsTab === 'mcp') renderSettingsPanel();
  } catch (e) { notice(e.message); }
}

const DL_FIELDS = [
  ['line_max_minutes', '单线路时限', '分钟', '单条线路超过该时间仍未下完，则放弃并切换下一条（共 3 条普通线路）。'],
  ['stall_seconds', '停滞判定', '秒', '下载进度超过该时间无变化，视为线路卡住并切换下一条。'],
  ['committed_stall_seconds', '收尾停滞判定', '秒', '进度达到“临近完成比例”后使用更宽松的停滞时间，避免在即将完成时放弃。'],
  ['committed_percent', '临近完成比例', '%', '进度达到该比例后，改用“收尾停滞判定”。'],
  ['slow_kbps', '低速提示阈值', 'KB/s', '低于该速度时仅在界面提示，不会切换线路。'],
  ['min_ok_kbps', '最低可接受速度', 'KB/s', '低于该速度才视为过慢。大文件会按“文件大小 ÷ 该速度”自动延长等待（最多 60 分钟），避免被固定时限误判。']
];
const HS_MODES = [['off', '关闭'], ['smart', '智能'], ['force', '强制']];

function tabDownload() {
  const d = state.download || {}, cfg = d.settings || {}, rng = d.ranges || {}, hs = d.highspeed || {};
  const rows = DL_FIELDS.map(([k, name, unit, hint]) => `<div class="setrow"><div><b>${name}</b><small>${hint}${rangeText(rng[k], unit, d.defaults?.[k])}</small></div>${numField('dl_' + k, cfg[k], rng[k] || [], name, unit)}</div>`).join('');
  const eta = `<div class="setrow"><div><b>预估超时切换</b><small>仅在高速通道为“智能”时生效。线路测速满 90 秒后，若按当前速度预计耗时超过“单线路时限”，则提前放弃并切换下一条，不等到超时。</small></div>${swLocal('dl_smart_eta_check', !!cfg.smart_eta_check, '预估超时切换')}</div>`;
  const probe = `<div class="setrow"><div><b>线路测速</b><small>开始下载时，先对 3 条普通线路各做短时测速，选用最快的一条。软件会记录各线路在不同时段的速度，之后同一时段优先使用历史最快的线路，测速时间也会缩短。测速会增加数十秒。下载中，若当前线路连续 1 分钟低于另一条线路历史速度的一半，才会切换（切换后需重新下载）。</small></div>${swLocal('dl_probe_enabled', !!cfg.probe_enabled, '线路测速')}</div>
<div class="setrow"><div><b>测速时长</b><small>每条线路的测速时间，越长越准确，但等待更久。${rangeText(rng.probe_seconds, '秒', d.defaults?.probe_seconds)}</small></div>${numField('dl_probe_seconds', cfg.probe_seconds, rng.probe_seconds || [], '测速时长', '秒')}</div>`;
  const fail = `<h4 style="margin-top:18px">失败处理</h4>
<div class="setrow"><div><b>自动重试轮数</b><small>下载未完成时，软件按 1、3、5、10 分钟的间隔自动重试，此后每 10 分钟一次。设为 0 表示不自动重试，失败后转为需处理。${rangeText(rng.auto_retry_max, '轮', d.defaults?.auto_retry_max)}</small></div>${numField('dl_auto_retry_max', cfg.auto_retry_max, rng.auto_retry_max || [], '自动重试轮数', '轮')}</div>
<div class="setrow"><div><b>普通线路失败上限</b><small>高速通道关闭时，普通线路累计失败达到该次数后，不再自动重试，转为需处理。${rangeText(rng.normal_max_failures, '次', d.defaults?.normal_max_failures)}</small></div>${numField('dl_normal_max_failures', cfg.normal_max_failures, rng.normal_max_failures || [], '普通线路失败上限', '次')}</div>
<div class="setrow"><div><b>高速尝试上限</b><small>同一文件的高速通道只扣一次积分，因此允许多次尝试；达到上限仍未完成则转为需处理。距科研通删除文件或自动采纳不足 2 小时，或遇到登录失效等需用户处理的问题时，也会立即转为需处理。${rangeText(rng.highspeed_max_tries, '次', d.defaults?.highspeed_max_tries)}</small></div>${numField('dl_highspeed_max_tries', cfg.highspeed_max_tries, rng.highspeed_max_tries || [], '高速尝试上限', '次')}</div>`;
  const hsName = (HS_MODES.find(m => m[0] === hs.mode) || [])[1] || '关闭';
  const sourceNote = hs.source === 'default' ? (hs.mode === 'force' ? '（积分达到 500，默认启用）' : '（默认设置）') : '';
  return `<h4>下载</h4>
<div class="callout">说明：“排队”表示已导入或同步、尚未开始下载。速度慢仅作提示，不会切换线路；卡住、超时、页面报错（及“智能”模式下的预估超时）才视为该线路失败。每个文件的高速通道最多点击一次。</div>
${rows}${eta}${probe}${fail}
<div class="actions"><button data-action="dl-save" class="primary">保存参数</button><button data-action="dl-reset">恢复默认</button></div>
<h4 style="margin-top:22px">高速通道</h4>
<div class="setrow"><div><b>高速通道模式</b><small>当前：${hsName}${sourceNote}。费用提示：积分低于 500 时，每个文件首次使用高速通道扣 2 积分，积分达到 500 免费；同一文件不重复扣分。关闭：仅使用 3 条普通线路，失败后转为需处理，不扣积分。智能：3 条普通线路均失败后，自动切换高速通道。强制：跳过普通线路，直接使用高速通道。选择智能或强制前，软件会核对科研通当前的费用规则，与上述不符则不使用。当前积分 ${esc(hs.points ?? '未知')}。</small></div>${seg('高速通道模式', HS_MODES, hs.mode || 'off', 'hs-mode')}</div>
${oaSettings()}`;
}

function oaSettings() {
  const p = state.pipeline || {}, prefs = state.preferences || {};
  const on = p.oa_first !== false, mode = prefs.oa_challenge_mode || 'prompt';
  const email = prefs.oa_contact_email || '';
  return `<h4 style="margin-top:22px">免费全文</h4>
<div class="setrow"><div><b>优先查找免费全文</b><small>开启后，带 DOI 的导入文献会先查找并下载合法的免费全文，本地核对通过即视为完成，不消耗积分；找不到或下载失败时，再向科研通求助。查找时会向 OpenAlex、Europe PMC、NCBI PMC、Semantic Scholar、Crossref、arXiv、bioRxiv 发送 DOI（填写联系邮箱后还会查询 Unpaywall）。关闭后不查找免费全文，导入后由用户决定是否求助。</small></div>${sw('oa-toggle', null, on, '优先查找免费全文')}</div>
<div class="setrow"><div><b>人机验证</b><small>遇到人机验证页时，软件不会自动绕过验证。“提示”：标记为需人工验证，由用户选择打开验证窗口或改走科研通。</small></div>${seg('人机验证处理方式', [['prompt', '提示'], ['popup', '弹出窗口'], ['skip', '改走科研通']], mode, 'oa-mode')}</div>
<h4 style="margin-top:22px">OpenAlex 文献搜索</h4>
<div class="setrow"><div><b>${state.openalex_key_saved ? 'OpenAlex 密钥已保存' : state.openalex_env_configured ? '使用环境变量中的 OpenAlex 密钥' : '尚未配置 OpenAlex 密钥'}</b><small>匿名查询使用共享额度；关键词游标翻页需要免费 API Key。语义搜索单次最多 50 条，按服务商额度计费。可在 <a href="https://openalex.org/settings/api" target="_blank" rel="noopener noreferrer">OpenAlex 申请免费密钥 ↗</a>。密钥使用 Windows 当前用户加密保存，不会再次显示；清除保存密钥后，如设置了环境变量仍会继续使用该环境变量。</small></div><div class="timerow"><input id="openalexKey" type="password" maxlength="256" autocomplete="off" placeholder="输入密钥" aria-label="OpenAlex API Key"><button data-action="openalex-key-save">保存</button><button data-action="openalex-key-clear" ${state.openalex_key_saved ? '' : 'disabled'}>清除</button></div></div>
<h4 style="margin-top:22px">CORE 密钥（可选）</h4>
<div class="setrow"><div><b>${p.core_key_configured ? 'CORE 密钥已设置' : 'CORE 密钥未设置'}</b><small>CORE 是免费论文库，使用密钥后才可查询；未设置时跳过该来源，其他来源不受影响。设置后，查找免费全文时会将 DOI 和密钥发送至 CORE（api.core.ac.uk）。密钥仅保存在本机，不会再次显示，也不会写入日志；留空保存即清除。可在 CORE 官网免费申请。</small></div><div class="timerow"><input id="coreKey" type="password" maxlength="200" autocomplete="off" placeholder="${p.core_key_configured ? '已保存，重新填写将替换' : '可不填'}" aria-label="CORE 密钥（可选）"><button data-action="core-key-save">保存</button><button data-action="core-key-clear" ${p.core_key_configured ? '' : 'disabled'}>清除</button></div></div>
<h4 style="margin-top:22px">查找缓存</h4>
<div class="setrow"><div><b>免费全文查找缓存</b><small>查找结果会在本机缓存（已找到约 1 天，未找到约 1 小时），以减少对各站点的访问。清除后将立即重新查询，已下载的文件不受影响。</small></div><button data-action="oa-cache-clear">清除缓存</button></div>
<h4 style="margin-top:22px">联系邮箱（可选）</h4>
<div class="setrow"><div><b>联系邮箱</b><small>填写后，查找时会将该邮箱随请求发送至 OpenAlex、Crossref 和 Unpaywall（对方对提供邮箱的请求放宽限流，Unpaywall 必须提供邮箱才可使用）。不填也可使用，但少一个查询来源，且更容易被限流。邮箱仅保存在本机，默认为空。</small></div><div class="timerow"><input id="oaEmail" type="email" maxlength="254" autocomplete="off" value="${esc(email)}" placeholder="可不填" aria-label="联系邮箱（可选）"><button data-action="oa-email-save">保存</button><button data-action="oa-email-clear" ${email ? '' : 'disabled'}>清除</button></div></div>`;
}

function tabReject() {
  const auto = state.preferences?.rejection_mode === 'auto';
  return `<h4>应助校验</h4>
<p>收到的应助文件与求助的文献不符时的处理方式。默认由用户自行判断。</p>
<div class="setrow"><div><b>自动驳回</b><small>${auto ? '已开启：仅当文件首页 DOI 明确为另一篇文献、且标题相似度低于 65% 时，自动向科研通提交“文件不一致”的驳回。扫描件不清晰、预印本、格式不符等情况仍需用户判断。注意：错误驳回会受到科研通处罚（扣分甚至封号），请先阅读规则。' : '已关闭：应助文件疑似不符时由用户决定是否驳回，软件不会自动驳回。开启后，软件会处理“首页 DOI 明确为另一篇文献”的情形；开启前需阅读规则与处罚。'}</small></div>${sw('reject-mode', null, auto, '自动驳回')}</div>`;
}

function raiseRow() {
  const pub = state.publish;
  if (!pub) return '';
  const r = pub.raise_settings, auto = r.mode === 'auto';
  return `<div class="setrow"><div><b>最低悬赏上调</b><small>部分文献（出版社、数据库等）要求的最低悬赏高于默认悬赏，科研通会提示或自动上调。“询问”：软件暂停并告知所需积分，由用户决定。“自动接受”：所需积分不超过上限、且支付后剩余积分不低于“保留积分”时自动接受，否则仍由用户决定。若科研通弹窗并非“上调最低悬赏”（如重复求助、文献信息冲突），无论如何设置均暂停并交由用户处理。${pub.batch_pending ? ' 积分达到 500 的批量求助页面尚未适配，目前仍逐条发布。' : ''}</small></div>${seg('最低悬赏上调处理方式', [['ask', '询问'], ['auto', '自动接受']], r.mode, 'raise-mode')}</div>
${auto ? `<div class="setrow"><div><b>自动接受上限</b><small>仅可选择官方档位，范围 ${esc(pub.raise_range[0])}–${esc(pub.raise_range[1])} 积分。当前：<b>${esc(r.max)}</b> 积分。</small></div>${rewardControl('raise_max', r.max)}</div>
<div class="setrow"><div><b>保留积分</b><small>自动接受后账户至少保留的积分，否则改为询问。${rangeText(pub.reserve_range, '积分', pub.defaults.reserve_points)}</small></div><div class="timerow">${numField('reservePoints', r.reserve, pub.reserve_range, '保留积分', '积分')}<button data-action="raise-reserve-save">保存</button></div></div>` : ''}`;
}

function tabPipeline() {
  const p = state.pipeline;
  if (!p || typeof p !== 'object') return '<h4>自动流程</h4><p class="hint">当前后台版本不提供自动流程。</p>';
  const row = (key, title, desc, on) => `<div class="setrow"><div><b>${title}</b><small>${desc}</small></div>${sw('pipe-toggle', key, !!on, title)}</div>`;
  return `<h4>自动流程</h4>
<p>当前积分 <b>${esc(p.points ?? '未知')}</b>，${p.tier === 'batch' ? '积分达到 500，可批量求助' : '积分低于 500，只能逐条求助'}。需先处理已收到的应助，才能继续求助；积分未知时按低于 500 处理。</p>
${row('enabled', '启用自动流程', '每 30 秒自动推进一步，仅执行无需用户决定的操作（查找免费全文、查看进展、下载等）。切换账户或重启软件后会自动关闭。', p.enabled)}
${row('auto_publish', '自动求助', '对已在确认清单中确认的文献，自动在科研通逐条发布求助并扣除积分；未确认的文献不会发布。费用提示：开启前需阅读规则。', p.auto_publish)}
${row('auto_adopt', '自动采纳', '下载后核对通过（DOI、标题、作者、摘要均一致）的应助将自动采纳，采纳后通常无法撤销；核对未通过的由用户判断。开启前需阅读规则。', p.auto_adopt)}
${rewardRow()}${raiseRow()}
<div class="setrow"><div><b>同步间隔</b><small>自动流程定期到科研通查看求助进展的间隔。范围 5–120 分钟，当前 ${esc(p.sync_minutes)} 分钟。</small></div><div class="timerow">${numField('syncMinutes', p.sync_minutes, [5, 120], '同步间隔', '分钟')}<button data-action="pipe-minutes">保存</button></div></div>${monitorSettings()}`;
}


function tabNotify() {
  const n = state.notify;
  if (!n) return '<h4>通知</h4><p class="hint">当前后台版本不提供通知设置。</p>';
  const cfg = n.settings, on = !!cfg.master;
  const rows = Object.entries(n.kinds).map(([k, name]) => {
    const v = !!cfg.items[k];
    return `<div class="setrow"><div><b>${esc(name)}</b><small>默认${n.defaults.items[k] ? '开启' : '关闭'}${on ? '' : '（总开关关闭时不弹出）'}</small></div>${sw('notify-item', k, v, name)}</div>`;
  }).join('');
  return `<h4>通知</h4>
<div class="callout">说明：系统通知由托盘图标弹出。关闭总开关后，托盘状态和右下角消息面板仍会更新，只是不再弹出系统通知。同一事件不会重复提醒。</div>
<div class="setrow"><div><b>系统通知</b><small>总开关，控制是否弹出全部系统通知。</small></div>${sw('notify-master', null, on, '系统通知')}</div>
<h4 style="margin-top:18px">通知类型${on ? '' : '（总开关已关闭，暂不生效）'}</h4>${rows}
<h4 style="margin-top:18px">免打扰</h4>
<div class="setrow"><div><b>免打扰时段</b><small>开启后，该时段内仅记录、不弹出通知，结束后汇总为一条提醒。可跨午夜，如 22:00–08:00。</small></div>${sw('notify-quiet-toggle', null, !!cfg.quiet_enabled, '免打扰时段')}</div>
<div class="setrow"><div><b>时间段</b><small>24 小时制，本机时间。${n.pending ? '当前有 ' + esc(n.pending) + ' 条待汇总。' : ''}</small></div><div class="timerow"><input id="quietStart" type="time" value="${esc(cfg.quiet_start)}" aria-label="免打扰开始"> – <input id="quietEnd" type="time" value="${esc(cfg.quiet_end)}" aria-label="免打扰结束"><button data-action="notify-quiet-save">保存</button></div></div>`;
}

function monitorSettings() {
  const m = state.monitor, a = state.autostart || {}, l = state.limits;
  if (!m) return '';
  const s = m.settings, r = m.ranges, lim = l?.settings || {}, lr = l?.ranges || {};
  const bandName = { hot: '刚发布（15 分钟内）', warm: '发布后 15–60 分钟', normal: '常规', idle: '无等待中的求助' }[m.band] || m.band;
  return `<h4 style="margin-top:22px">后台监控（只读）</h4>
<div class="callout monnote">说明：关闭窗口后，后台监控仍会定期查看求助进展，可从托盘退出。它只读取“我的求助”列表，不发布、不采纳、不驳回，也不消耗积分。与“自动流程”相互独立：后台监控重启后保持原设置，自动流程重启后恢复关闭。</div>
<div class="setrow"><div><b>后台监控</b><small>当前：${esc(m.label)}。开启后，定期在后台查看求助，发现应助、登录失效等变化时按“通知”设置提醒；自动流程开启时，还会接着执行下载。${m.session ? '' : '尚未检测到科研通登录，登录后才会同步。'}</small></div>${sw('mon-toggle', null, !!m.enabled, '后台监控')}</div>
<div class="setrow"><div><b>立即同步</b><small>读取一页求助列表，仍遵守下方的请求间隔。</small></div><button data-action="mon-sync">立即同步</button></div>
<div class="setrow"><div><b>初期加频</b><small>应助通常在发布后几分钟内出现。开启后，发布后前 ${esc(s.hot_fast_min)} 分钟每 ${esc(s.hot_fast_seconds)} 秒查看一次，之后至 15 分钟每 1 分钟、15–60 分钟每 3 分钟一次，再往后按“常规间隔”。每次仅读取一页列表，多条求助同时处于该阶段时也只查看一次；无人等待应助时不再加频；查看失败时退回每 3 分钟。关闭后始终按固定间隔。当前：${esc(bandName)}，约每 ${esc(m.interval_seconds)} 秒。</small></div>${sw('mon-hot-toggle', null, !!s.hot_enabled, '初期加频')}</div>
<div class="setrow"><div><b>加频参数</b><small>加频阶段的查看间隔范围 ${esc(r.hot_fast_seconds[0])}–${esc(r.hot_fast_seconds[1])} 秒（默认 ${esc(m.defaults.hot_fast_seconds)}），持续时间范围 ${esc(r.hot_fast_min[0])}–${esc(r.hot_fast_min[1])} 分钟（默认 ${esc(m.defaults.hot_fast_min)}）。</small></div><div class="timerow">${numField('monFastSec', s.hot_fast_seconds, r.hot_fast_seconds, '加频间隔', '秒')} ${numField('monFastMin', s.hot_fast_min, r.hot_fast_min, '加频时长', '分钟')}</div></div>
<div class="setrow"><div><b>常规间隔</b><small>没有刚发布的求助时使用。${rangeText(r.normal_min, '分钟', m.defaults.normal_min)}查看失败后会逐步放慢（10→20→40 分钟，最慢 60 分钟）；无等待中的求助时每 30 分钟一次。</small></div><div class="timerow">${numField('monNormal', s.normal_min, r.normal_min, '常规间隔', '分钟')}<button data-action="mon-save">保存监控参数</button></div></div>
<div class="setrow"><div><b>开机自启</b><small>开机后自动在后台启动，仅启动服务和托盘图标，不弹出窗口。通过在“启动”文件夹放置快捷方式实现，不修改注册表，无需管理员权限。默认关闭。${a.available === false ? '当前为隔离运行环境，不可用。' : ''}</small></div>${sw('autostart-toggle', null, !!a.enabled, '开机自启', a.available === false ? 'disabled' : '')}</div>
${l ? `<h4 style="margin-top:22px">请求节奏</h4>
<div class="setrow"><div><b>最小请求间隔</b><small>访问科研通时一次只执行一项操作，两次之间至少间隔该时间，以减轻对科研通的压力。${rangeText(lr.request_gap_seconds, '秒', l.defaults.request_gap_seconds)}下载同样一次只下载一个。</small></div><div class="timerow">${numField('limGap', lim.request_gap_seconds, lr.request_gap_seconds, '最小请求间隔', '秒')}</div></div>
<div class="setrow"><div><b>每小时求助上限</b><small>${rangeText(lr.publish_per_hour, '条', l.defaults.publish_per_hour)}达到上限后，自动流程等待下一小时再发布。最近 1 小时已发布 ${esc(l.publish_recent)} 条。</small></div><div class="timerow">${numField('limHour', lim.publish_per_hour, lr.publish_per_hour, '每小时求助上限', '条')}</div></div>
<div class="setrow"><div><b>求助最小间隔</b><small>两次发布求助之间的最短间隔。${rangeText(lr.publish_gap_seconds, '秒', l.defaults.publish_gap_seconds)}</small></div><div class="timerow">${numField('limPubGap', lim.publish_gap_seconds, lr.publish_gap_seconds, '求助最小间隔', '秒')}<button data-action="limits-save">保存节奏</button></div></div>` : ''}`;
}

function tabAccounts() {
  const list = (state.accounts || []).map(a => `<div class="accountrow"><span><b>${esc(a.label)}</b>${a.credentials_saved ? ' <small class="hint">已保存凭据</small>' : ''}</span><button data-action="switch-account" data-id="${esc(a.id)}" ${a.active ? 'disabled' : ''}>${a.active ? '当前账户' : '切换'}</button></div>`).join('');
  return `<h4>账户</h4>
<div class="callout">注意：每个账户须为本人注册，不得替他人求助、转移积分或使用多个账号。</div>
<p>切换账户不会弹出登录窗口。各账户的登录状态、已保存的凭据和求助队列相互独立。</p>${list}
<label>新账户名称（备注）<input id="newAccountName" maxlength="60" placeholder="例如：工作账户"></label>
<div class="actions"><button data-action="add-account" class="primary">添加并切换</button></div>
<h4 style="margin-top:22px">已保存的凭据</h4>
<p class="hint">${state.credentials_saved ? '当前账户的邮箱和密码已使用 Windows 用户加密保存。' : '当前账户没有已保存的凭据。'}</p>
<div class="actions"><button data-action="forget-login" class="danger" ${state.credentials_saved ? '' : 'disabled'}>删除已保存的凭据</button></div>`;
}

/* ---------- other dialogs ---------- */
function openLogin() {
  openDialog('登录当前账户', `<p>验证码或微信扫码需在用户主动打开的登录窗口中完成。各账户的登录状态与加密保存的账号密码相互独立。</p><div class="actions"><button data-action="login-window" class="primary">打开登录窗口</button><button data-action="saved-login" ${state.credentials_saved ? '' : 'disabled'}>使用已保存的凭据</button></div><details><summary>邮箱密码登录</summary><label>邮箱<input id="loginEmail" type="email" autocomplete="off"></label><label>密码<input id="loginPassword" type="password" autocomplete="off"></label><div class="setrow"><div><b>保存凭据</b><small>使用 Windows 用户加密保存到当前账户，下次可直接使用。</small></div>${swLocal('rememberLogin', false, '保存凭据')}</div><div class="actions"><button data-action="password-login">登录</button><button data-action="forget-login" class="danger">删除已保存的凭据</button></div></details>`);
}

function openPipeRules(key) {
  const text = key === 'auto_publish'
    ? '<p><b>费用提示：</b>开启后，已在确认清单中确认的文献将自动在科研通发布求助，并扣除积分；未确认的文献不会发布。</p><ul><li>积分低于 500：一次只发布一条；需先处理已收到的应助，才能继续求助。</li><li>积分达到 500：逐条发布，无需等待应助。</li><li>发布结果不确定时，记为“未确认”，不会自动重试。</li></ul><p class="hint">说明：自动求助功能尚未在真实页面上完整验收。</p>'
    : '<p>开启后，核对通过的应助（身份匹配且公开书目一致）将被自动采纳。</p><ul><li>采纳后通常无法撤销。</li><li>核对未通过或信息缺失时不会自动采纳，交由用户判断。</li><li>未出现确认文字时，记为“未确认”，不会自动重试。</li></ul><p class="hint">说明：自动采纳功能尚未在真实页面上完整验收。</p>';
  openDialog(key === 'auto_publish' ? '开启自动求助' : '开启自动采纳', `${text}<button data-action="confirm-pipe" data-id="${key}" class="primary">已了解规则，确认开启</button>`);
}

/* ---------- import ---------- */
async function previewImport(route, body) {
  const seq = ++previewSequence;
  try {
    const result = await post(route, body);
    if (seq !== previewSequence) return;
    importRecords = result.records;
    importUnparsed = result.unparsed || [];
    syncAiButtons();
    $('importPreview').innerHTML = `<b>本地识别 ${importRecords.length} 篇 · 未使用 AI</b>` + importRecords.slice(0, 5).map((r, n) =>
      `<p>${n + 1}. ${esc(r.title || '标题待补充')}<br>DOI：${esc(r.doi || '未识别')}<br>作者：${esc(r.authors.join('；') || '待补充')}</p>`).join('');
  } catch (e) { importRecords = null; notice(e.message); }
}

$('input').oninput = () => {
  importRecords = null; importUnparsed = []; ++previewSequence;
  syncAiButtons();
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => previewImport('parse', { text: $('input').value }), 450);
};
$('accountSelect').onchange = () => action('accounts/switch', { account_id: $('accountSelect').value });
$('textfile').onchange = e => {
  const file = e.target.files[0];
  if (!file) return;
  if (file.size > 10000000) return notice('文件大小不能超过 10 MB');
  const reader = new FileReader();
  reader.onload = () => { previewImport('parse-file', { filename: file.name, base64: String(reader.result).split(',')[1] }); $('input').value = ''; };
  reader.readAsDataURL(file);
  e.target.value = '';
};


/* ---------- action dispatcher ---------- */
async function run(a, id) {
  switch (a) {
    case 'close-dialog': return closeDialog();
    case 'close-settings': return mHide('settings');
    case 'settings': return openSettings(id);
    case 'tab': settingsTab = id; renderSettingsPanel(); if (id === 'mcp') loadMCP(); if (id === 'logs') loadLogs(); return;
    case 'filter': filter = id; return render();
    case 'demo': return action('demo');
    case 'choose-file': return $('textfile').click();
    case 'import': {
      if (!importRecords?.length && !$('input').value.trim()) return notice('请先粘贴文献或选择文件');
      if (await action('import', importRecords ? { records: importRecords } : { text: $('input').value })) {
        $('input').value = ''; importRecords = null; $('importPreview').textContent = '';
      }
      return;
    }
    case 'login': return openLogin();
    case 'login-window': case 'saved-login':
      if (await action('browser', { action: 'login', ...(a === 'saved-login' ? { use_saved: true } : {}) })) closeDialog();
      return;
    case 'password-login': {
      const body = { action: 'login', email: $('loginEmail').value.trim(), password: $('loginPassword').value, save_credentials: isOn('rememberLogin') };
      $('loginPassword').value = '';
      if (await action('browser', body)) closeDialog();
      return;
    }
    case 'forget-login': if (await action('forget-credentials') && mOpen('dialog')) openLogin(); return;
    case 'add-account': if (await action('accounts/add', { label: $('newAccountName').value })) { lastPanel = ''; renderSettingsPanel(); } return;
    case 'switch-account': if (await action('accounts/switch', { account_id: id })) { lastPanel = ''; renderSettingsPanel(); } return;
    case 'ai-toggle':
      if (aiOn()) { if (await action('ai/settings', { enabled: false })) { lastPanel = ''; renderSettingsPanel(); } return; }
      openDialog('开启 AI 辅助', '<div class="callout"><b>费用提示：</b>AI 请求由所配置的服务商计费，费用由用户自行承担。</div><p><b>隐私提示：</b>开启后，经逐次确认的内容会发送至所配置的 AI 服务；每次发送前都会展示预览，确认后才会发送。AI 输出可能有误，仅作人工辅助。</p><p class="hint">请先在设置中填写接口与 API Key。</p><button data-action="enable-ai" class="primary">已了解，确认开启</button>');
      return;
    case 'enable-ai': {
      const body = aiFormBody(true, true);
      if (await action('ai/settings', body)) { $('aiKey').value = ''; closeDialog(); lastPanel = ''; renderSettingsPanel(); notice('AI 辅助已开启'); }
      return;
    }
    case 'ai-save': {
      const body = aiFormBody(true, true);
      if (await action('ai/settings', body)) { $('aiKey').value = ''; lastPanel = ''; renderSettingsPanel(); notice('AI 设置已保存'); }
      return;
    }
    case 'ai-forget': if (await action('ai/forget-key')) { lastPanel = ''; renderSettingsPanel(); notice('已删除保存的 AI 密钥'); } return;
    case 'ai-test': {
      const body = aiFormBody(false); delete body.enabled; delete body.consent; delete body.storage;
      $('aiTestResult').textContent = '正在连接…（仅查询模型列表）';
      try {
        const r = await post('ai/test', body);
        $('aiTestResult').textContent = `连接成功，共 ${r.count} 个模型${r.model_listed === false ? '，但未找到所填模型名' : ''}。${r.note}`;
      } catch (e) { $('aiTestResult').textContent = '连接失败：' + e.message; }
      return;
    }
    case 'ai-organize': {
      if (!aiOn()) return notice(AI_OFF_HINT);
      if (!importUnparsed.length) return notice('没有本地未识别的内容');
      organizeCtx = { segments: importUnparsed.map(u => u.text), result: null };
      openDialog('用 AI 整理', `<p><b>隐私提示：</b>仅发送下列文字至 ${esc(state.ai.provider_label)} / ${esc(state.ai.model)}，不发送其他文献、文件、路径或账号信息；可能产生 API 费用。</p><div class="sendpreview">${organizeCtx.segments.map(t => `<pre>${esc(t)}</pre>`).join('')}</div><label class="check"><input id="orgConsent" type="checkbox">同意发送上述文字</label><button data-action="submit-organize" class="primary">发送并整理</button>`);
      return;
    }
    case 'submit-organize': {
      try {
        const r = await post('ai/organize', { segments: organizeCtx.segments, consent: $('orgConsent').checked });
        organizeCtx.result = r;
        const rows = r.records.map((x, n) => `<p>${n + 1}. ${esc(x.title || '标题待补充')}<br>DOI：${esc(x.doi || '未识别')}<br>作者：${esc((x.authors || []).join('；') || '待补充')}</p>`).join('');
        openDialog('确认整理结果', `<p class="hint">AI 输出可能有误，请逐条核对。确认后才会加入队列，并仍经过本地校验；仅保留原文中实际出现的 DOI。</p>${rows || '<p>未整理出可用记录。</p>'}<div class="actions"><button data-action="confirm-organize" class="primary" ${r.records.length ? '' : 'disabled'}>确认导入 ${r.records.length} 条</button><button data-action="close-dialog">取消</button></div>`);
      } catch (e) { notice(e.message); }
      return;
    }
    case 'confirm-organize': {
      if (await action('import', { records: organizeCtx.result.records, ai_assisted: true })) { closeDialog(); importUnparsed = []; organizeCtx = null; syncAiButtons(); notice('已加入队列'); }
      return;
    }
    case 'logs-load': return loadLogs();
    case 'report-build': {
      try {
        const r = await post('report/build', { error_ids: [...logsState.selected] });
        Object.assign(logsState, { report: r.text, reportId: r.report_id, analysis: null, consent: false });
        lastPanel = ''; renderSettingsPanel();
      } catch (e) { notice(e.message); }
      return;
    }
    case 'report-ai': {
      if (!$('reportConsent')?.checked) return notice('请先勾选同意发送报告预览');
      try { logsState.analysis = await post('report/analyze', { report_id: logsState.reportId, consent: true }); lastPanel = ''; renderSettingsPanel(); await refresh(); }
      catch (e) { notice(e.message); }
      return;
    }
    case 'report-save': {
      const d = new Date(), pad = n => String(n).padStart(2, '0');
      downloadText(`kytdl-report-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.md`, logsState.report || '');
      return;
    }
    case 'report-issue': {
      try { const r = await post('report/issue'); window.open(r.url, '_blank', 'noopener'); }
      catch (e) { notice(e.message); }
      return;
    }
    case 'ack-disclaimer': if (await action('preferences', { disclaimer_ack: true })) mHide('disclaimer'); return;
    case 'show-first-run': return openDisclaimer();
    case 'show-full-disclaimer': { mHide('disclaimer'); return openSettings('about'); }
    case 'copy-mcp-json': case 'copy-mcp-toml':
      try { await navigator.clipboard.writeText(a === 'copy-mcp-json' ? JSON.stringify(mcpConfig.claude, null, 2) : mcpConfig.codex); notice('配置已复制'); }
      catch (e) { notice('请在配置框中手动复制'); }
      return;
    case 'toggle-mcp':
      if (state.preferences?.mcp_website_actions) { await action('preferences', { mcp_website_actions: false }); return; }
      openDialog('允许 MCP 操作科研通', '<p><b>注意：</b>开启后，所信任的外部工具可调用当前账户的同步、登录、签到、下载和驳回操作。高速通道及自动驳回仍遵循已单独开启的规则。外部工具可能将返回的信息发送至其云端模型。</p><button data-action="confirm-mcp" class="primary">已了解，确认开启</button>');
      return;
    case 'confirm-mcp': if (await action('preferences', { mcp_website_actions: true })) closeDialog(); return;
    case 'hs-mode': {
      const hs = state.download?.highspeed || {};
      if (id === hs.mode && hs.source === 'user') return;
      const free = Number.isInteger(hs.points) && hs.points >= (hs.force_default_points || 500);
      if (id === 'off' || free) return action('preferences', { download_highspeed: id, rules_version: RULES_VERSION });
      const warn = id === 'force' ? '<div class="callout"><b>费用提示：</b>每个文件首次使用高速通道扣 2 积分，批量求助时费用会累计。</div>' : '';
      openDialog(id === 'force' ? '启用强制高速' : '启用智能高速', `<p>${id === 'force' ? '强制：跳过普通线路，直接使用高速通道。' : '智能：3 条普通线路均失败（含按当前速度预计超出单线路时限）后，才使用高速通道。'}</p>${warn}<ul><li>积分低于 500：每个文件首次使用高速通道扣 2 积分，同一文件不重复扣分。</li><li>积分达到 500：免费。</li><li>科研通费用规则与上述不符、入口不明或点击停止时，不使用高速通道。</li></ul><p>不会更改科研通账户本身的下载设置。</p><button data-action="confirm-hs" data-id="${esc(id)}" class="primary">已了解规则，确认启用</button>`);
      return;
    }
    case 'confirm-hs': if (await action('preferences', { download_highspeed: id, rules_version: RULES_VERSION })) closeDialog(); return;
    case 'dl-save': {
      const settings = {};
      for (const [k, name] of [...DL_FIELDS.map(f => [f[0], f[1]]), ['auto_retry_max', '自动重试轮数'], ['normal_max_failures', '普通线路失败上限'], ['highspeed_max_tries', '高速尝试上限']]) {
        const v = Number($('dl_' + k).value);
        const r = state.download?.ranges?.[k] || [];
        if (!Number.isInteger(v) || v < r[0] || v > r[1]) return notice(`${name}须为 ${r[0]}–${r[1]} 的整数`);
        settings[k] = v;
      }
      settings.smart_eta_check = isOn('dl_smart_eta_check');
      settings.probe_enabled = isOn('dl_probe_enabled');
      const ps = Number($('dl_probe_seconds').value), pr = state.download?.ranges?.probe_seconds || [5, 60];
      if (!Number.isInteger(ps) || ps < pr[0] || ps > pr[1]) return notice(`测速时长须为 ${pr[0]}–${pr[1]} 的整数`);
      settings.probe_seconds = ps;
      if (await action('preferences', { download_settings: settings })) { lastPanel = ''; renderSettingsPanel(); notice('下载参数已保存'); }
      return;
    }
    case 'dl-reset': if (await action('preferences', { download_reset: true })) { lastPanel = ''; renderSettingsPanel(); notice('已恢复默认参数'); } return;
    case 'oa-accept': {
      const item = findItem(id);
      openDialog('保留免费全文为完成', `<p>${esc(item?.title)}</p><p>本地核对未完全匹配：${esc(human(item?.verification?.reason || ''))}</p><p>确认后，该条记为已完结，不再向科研通求助。</p><button data-action="submit-oa-accept" data-id="${esc(id)}" class="primary">确认保留</button>`);
      return;
    }
    case 'submit-oa-accept': if (await action('oa-accept', { id, confirmed: true })) closeDialog(); return;
    case 'oa-discard': return action('oa-discard', { id });
    case 'reject-mode':
      if (state.preferences?.rejection_mode === 'auto') return action('preferences', { rejection_mode: 'manual' });
      openDialog('开启自动驳回', '<p>仅对用户本人未完结、由软件下载并核对过的应助生效：文件首页 DOI 明确为另一篇文献、且标题相似度低于 65% 时，才会自动提交“文件不一致”的驳回。</p><div class="callout"><b>注意：</b>作者或摘要缺失、接口失败、文字识别失败时不会自动驳回。科研通入口变化或结果不明时会停止，不重复提交。错误驳回会受到科研通处罚（扣分甚至封号）。</div><p class="hint">说明：驳回功能尚未在未完结的应助上实测，不代表整个流程已验证通过。</p><button data-action="confirm-auto-reject" class="primary">已了解规则，确认开启</button>');
      return;
    case 'confirm-auto-reject': if (await action('preferences', { rejection_mode: 'auto', rejection_rules_version: RULES_VERSION })) closeDialog(); return;
    case 'reject': {
      rejectTarget = id;
      const item = findItem(id), rr = state.reject_rules || { categories: [], not_allowed: [], penalties: [], guidance: '' };
      const cats = rr.categories.map((c, n) => `<label class="check"><input type="radio" name="rejectCat" value="${esc(c.key)}" ${item?.rejection?.category === c.key ? 'checked' : ''}> <b>${esc(c.label)}</b><br><small>${esc(c.hint)}</small></label>`).join('');
      openDialog('驳回应助', `<p>${esc(item?.title)}</p><div class="callout"><b>请先阅读规则：</b>${esc(rr.guidance)}</div><p><b>科研通规定不得驳回的情形：</b></p><ul>${rr.not_allowed.map(t => `<li>${esc(t)}</li>`).join('')}</ul><p><b>错误驳回的处罚：</b></p><ul class="warn">${rr.penalties.map(t => `<li>${esc(t)}</li>`).join('')}</ul><p><b>仅可选择以下 4 类理由之一：</b></p><div id="rejectCats">${cats}</div><label>具体说明（必填，至少 ${esc(rr.detail_min)} 字）<textarea id="rejectDetail" maxlength="${esc(rr.detail_max)}" placeholder="例如：文件首页为另一篇文献，DOI 为 10.xxxx/yyyy"></textarea></label><button data-action="submit-reject" class="danger">确认驳回</button>`);
      return;
    }
    case 'submit-reject': {
      const cat = document.querySelector('input[name="rejectCat"]:checked')?.value;
      if (!cat) return notice('请选择一类驳回理由');
      if (await action('browser', { action: 'reject', id: rejectTarget, category: cat, detail: $('rejectDetail').value.trim(), confirmed: true })) closeDialog();
      return;
    }
    case 'publish': {
      const item = findItem(id), reward = item?.request_reward || state.reward?.default || 10;
      const raise = state.publish?.raise_settings, raiseText = raise?.mode === 'auto' ? `自动接受，上限 ${raise.max} 积分（保留 ${raise.reserve} 积分）` : '询问';
      openDialog('确认发布求助', `<p><b>${esc(item?.title || '（标题待补充）')}</b></p><table class="plain"><tr><td>DOI</td><td>${esc(item?.doi || '无')}</td></tr><tr><td>悬赏</td><td>${esc(reward)} 积分（最低悬赏被上调时：${esc(raiseText)}）</td></tr><tr><td>自动关闭</td><td>科研通默认 5 天后关闭；到期无人应助则退还积分</td></tr></table><p><b>费用提示：</b>发布将扣除积分。软件会先用 DOI 调用科研通“智能提取”，再核对返回的 DOI、标题、相似度和类型，任一项不符即停止，不扣积分。需先处理已收到的应助，才能继续求助（积分低于 500 时一次只能求助一条）。${state.publish?.batch_pending ? '积分达到 500 的批量求助尚未适配，目前仍逐条发布。' : ''}</p><p class="hint">无 DOI、非英文、补充材料或非期刊论文，不会自动求助。发布结果不确定时不会自动重试。</p><button data-action="submit-publish" data-id="${esc(id)}" class="primary">确认发布</button>`);
      return;
    }
    case 'submit-publish': if (await action('browser', { action: 'publish', id, confirmed: true })) closeDialog(); return;
    case 'adopt': {
      const item = findItem(id);
      openDialog('确认采纳', `<p>${esc(item?.title)}</p><p><b>注意：</b>采纳后通常无法撤销。请确认已查看并核对该应助 PDF 与求助内容一致。</p><button data-action="submit-adopt" data-id="${esc(id)}" class="primary">确认采纳</button>`);
      return;
    }
    case 'submit-adopt': if (await action('browser', { action: 'adopt', id, confirmed: true })) closeDialog(); return;
    case 'ai-recognize': {
      if (!aiOn()) return notice(AI_OFF_HINT);
      aiTarget = id;
      const item = findItem(id), pages = Math.min(3, item?.verification?.pages || 1);
      openDialog('确认发送页图', `<p><b>隐私提示：</b>仅发送所选页图至已配置的 AI 服务，不发送账号、Cookie 或本地路径。最多 3 页；费用按 API 账户计费。</p><div class="pagepreviews">${Array.from({ length: pages }, (_, n) => `<label><input type="checkbox" class="ai-page" value="${n}" ${n === 0 ? 'checked' : ''}> 第 ${n + 1} 页<img src="/pdf-preview/${encodeURIComponent(id)}?page=${n}" alt="第 ${n + 1} 页预览"></label>`).join('')}</div><label class="check"><input id="sendAIConsent" type="checkbox">同意将所选页图发送至该 AI 服务，并自行承担 API 费用</label><button data-action="submit-ai" class="primary">发送并识别</button>`);
      return;
    }
    case 'submit-ai': {
      const pages = [...document.querySelectorAll('.ai-page:checked')].map(x => Number(x.value));
      if (await action('ai/recognize', { id: aiTarget, pages, consent: $('sendAIConsent').checked })) closeDialog();
      return;
    }
    case 'pipe-toggle': {
      const p = state.pipeline || {};
      if (id === 'enabled') return action('pipeline', { enabled: !p.enabled });
      if (p[id]) return action('pipeline', { [id]: false });
      return openPipeRules(id);
    }
    case 'confirm-pipe': if (await action('pipeline', { [id]: true, rules_version: state.pipeline?.rules_version })) closeDialog(); return;
    case 'pipe-minutes': {
      const n = Number($('syncMinutes').value);
      if (!Number.isInteger(n) || n < 5 || n > 120) return notice('同步间隔须为 5–120 的整数');
      return action('pipeline', { sync_minutes: n });
    }
    case 'pipeline-step': return action('pipeline/step');
    case 'notify-master': return action('notifications', { master: !state.notify?.settings?.master });
    case 'notify-item': return action('notifications', { items: { [id]: !state.notify?.settings?.items?.[id] } });
    case 'notify-quiet-toggle': return action('notifications', { quiet_enabled: !state.notify?.settings?.quiet_enabled });
    case 'notify-quiet-save': return action('notifications', { quiet_start: $('quietStart').value, quiet_end: $('quietEnd').value });
    case 'mon-toggle': return action('monitor', { enabled: !state.monitor?.enabled });
    case 'mon-sync': return action('monitor', { sync_now: true });
    case 'mon-hot-toggle': return action('monitor', { hot_enabled: !state.monitor?.settings?.hot_enabled });
    case 'mon-save': {
      const body = { hot_fast_seconds: Number($('monFastSec').value), hot_fast_min: Number($('monFastMin').value), normal_min: Number($('monNormal').value) };
      const names = { hot_fast_seconds: '加频间隔', hot_fast_min: '加频时长', normal_min: '常规间隔' };
      const r = state.monitor?.ranges || {};
      for (const k of Object.keys(body)) {
        if (!Number.isInteger(body[k]) || body[k] < r[k][0] || body[k] > r[k][1]) return notice(`${names[k]}须为 ${r[k][0]}–${r[k][1]} 的整数`);
      }
      return action('monitor', body);
    }
    case 'autostart-toggle': return action('autostart', { enabled: !state.autostart?.enabled });
    case 'limits-save': {
      const body = { request_gap_seconds: Number($('limGap').value), publish_per_hour: Number($('limHour').value), publish_gap_seconds: Number($('limPubGap').value) };
      const names = { request_gap_seconds: '最小请求间隔', publish_per_hour: '每小时求助上限', publish_gap_seconds: '求助最小间隔' };
      const r = state.limits?.ranges || {};
      for (const k of Object.keys(body)) {
        if (!Number.isInteger(body[k]) || body[k] < r[k][0] || body[k] > r[k][1]) return notice(`${names[k]}须为 ${r[k][0]}–${r[k][1]} 的整数`);
      }
      return action('limits', body);
    }
    case 'upload': {
      const input = document.createElement('input');
      input.type = 'file'; input.accept = '.pdf';
      input.onchange = () => {
        const file = input.files[0];
        if (!file) return;
        if (file.size > 25000000) return notice('PDF 大小不能超过 25 MB');
        const reader = new FileReader();
        reader.onload = () => action('upload', { id, base64: String(reader.result).split(',')[1] });
        reader.readAsDataURL(file);
      };
      input.click();
      return;
    }
    case 'metadata': case 'verify': return action(a, { id });
    case 'oa-fetch': case 'oa-human': case 'oa-skip': return action(a, { id });
    case 'oa-lookup': return action('oa-fetch', { id, lookup_only: true });
    case 'oa-download': {
      const [itemId, n] = String(id).split('|');
      const link = (findItem(itemId)?.oa_check?.links || [])[Number(n)];
      if (!link) return notice('未找到该来源，请重新查找');
      return action('oa-fetch', { id: itemId, url: link.url });
    }
    case 'open-file': { const [itemId, target] = String(id).split('|'); return action('open', { id: itemId, target }); }
    case 'ack-item': return action('item/ack', { id });
    case 'reset-publish':
      openDialog('重置发布记录', `<p>${esc(findItem(id)?.title || '')}</p><p><b>注意：</b>仅当已在科研通“我的求助”中确认<b>不存在该求助</b>时，才可确认。重置后该条回到排队状态，之后可能再次发布并扣除积分。</p><button data-action="submit-reset-publish" data-id="${esc(id)}" class="danger">确认：未发出，重置</button>`);
      return;
    case 'submit-reset-publish': if (await action('item/reset-publish', { id, confirmed: true })) closeDialog(); return;
    case 'confirm-published':
      openDialog('确认已发出', `<p>${esc(findItem(id)?.title || '')}</p><p>请确认已在科研通“我的求助”中看到该求助。软件将立即同步一次，按标题对应该条，不会再次发布。</p><button data-action="submit-confirm-published" data-id="${esc(id)}" class="primary">确认：已发出</button>`);
      return;
    case 'submit-confirm-published': if (await action('item/confirm-published', { id, confirmed: true })) closeDialog(); return;
    case 'handover-done': return action('item/handover-done', { id });
    case 'handover-retry': return action('item/handover-retry', { id });
    case 'pipeline-enable': return action('pipeline', { enabled: true });
    case 'core-key-save': {
      const key = $('coreKey').value.trim();
      if (!key) return notice('请先填写密钥；清除请点击“清除”');
      $('coreKey').value = '';
      if (await action('pipeline', { core_api_key: key })) { lastPanel = ''; renderSettingsPanel(); notice('CORE 密钥已保存（不再显示）'); }
      return;
    }
    case 'core-key-clear': if (await action('pipeline', { core_api_key: '' })) { lastPanel = ''; renderSettingsPanel(); notice('CORE 密钥已清除'); } return;
    case 'openalex-key-save': {
      const key = $('openalexKey').value; $('openalexKey').value = '';
      if (await action('openalex-key', {key})) { lastPanel = ''; renderSettingsPanel(); notice('OpenAlex 密钥已加密保存'); }
      return;
    }
    case 'openalex-key-clear': if (await action('openalex-key-clear')) { lastPanel = ''; renderSettingsPanel(); notice('已清除保存的 OpenAlex 密钥'); } return;
    case 'oa-cache-clear': if (await action('oa-cache-clear')) notice('已清除免费全文查找缓存'); return;
    case 'raise-mode':
      if (id === 'ask') return action('pipeline', { raise_mode: 'ask' });
      if (state.publish?.raise_settings?.mode === 'auto') return;
      openDialog('自动接受最低悬赏上调', '<p><b>费用提示：</b>开启后，当科研通要求的最低悬赏高于所设悬赏时：</p><ul><li>仅当所需积分不超过上限、且支付后剩余积分不低于保留积分时，才会自动接受并多支付积分；</li><li>超出时仍暂停并交由用户处理；</li><li>弹窗并非单纯的“上调最低悬赏”（如重复求助、信息冲突）时，始终暂停并交由用户处理；</li><li>每次实际支出都会记录在该求助和消息中。</li></ul><button data-action="confirm-raise" class="primary">已了解规则，确认开启</button>');
      return;
    case 'confirm-raise': if (await action('pipeline', { raise_mode: 'auto', raise_confirmed: true })) closeDialog(); return;
    case 'raise-reserve-save': {
      const n = Number($('reservePoints').value), r = state.publish?.reserve_range || [0, 5000];
      if (!Number.isInteger(n) || n < r[0] || n > r[1]) return notice(`保留积分须为 ${r[0]}–${r[1]} 的整数`);
      return action('pipeline', { reserve_points: n });
    }
    case 'oa-toggle': return action('pipeline', { oa_first: state.pipeline?.oa_first === false });
    case 'oa-mode': return action('pipeline', { oa_challenge_mode: id });
    case 'oa-email-save': if (await action('pipeline', { oa_contact_email: $('oaEmail').value })) { lastPanel = ''; renderSettingsPanel(); notice('联系邮箱已保存'); } return;
    case 'oa-email-clear': if (await action('pipeline', { oa_contact_email: '' })) { lastPanel = ''; renderSettingsPanel(); notice('联系邮箱已清除'); } return;
    case 'stop': return action('stop');
    case 'hide-home-notice': return action('preferences', { home_notice_hidden: true });
    case 'show-home-notice': return action('preferences', { home_notice_hidden: false });
    case 'open-batch': if (mOpen('settings')) mHide('settings'); return openIntake(id);
    case 'focus': return focusItem(id);
    case 'intake-close': case 'intake-later': {
      const bid = intakeCtx?.batchId;
      mHide('intake');
      if (a === 'intake-later' && bid) await action('batch/defer', { batch_id: bid });
      return;
    }
    case 'intake-csv': return intakeCsv();
    case 'intake-confirm': {
      if (!intakeCtx || !intakeCtx.checked.size) return notice('请至少选择一条');
      const ids = [...intakeCtx.checked], rewards = Object.fromEntries(ids.map(i => [i, intakeCtx.rewards[i]]));
      if (await action('batch/confirm', { batch_id: intakeCtx.batchId, item_ids: ids, rewards })) { mHide('intake'); notice(`已确认 ${ids.length} 条，将按自动流程的设置向科研通求助`); }
      return;
    }
    case 'shutdown': return action('shutdown');
    case 'sync': case 'signin': case 'prepare': case 'review': case 'download':
      return action('browser', { action: a, id });
  }
}

document.body.onclick = async event => {
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  if (button.tagName === 'A' || button.tagName === 'INPUT') event.preventDefault();   // switches and radios only show the state held by the backend
  try { await run(button.dataset.action, button.dataset.filter || button.dataset.id); }
  catch (e) { notice(e.message); }
};

// Arrow-key navigation between settings tabs.
document.addEventListener('keydown', e => {
  const tab = e.target.closest?.('.tabs [role=tab]');
  if (!tab || !['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'].includes(e.key)) return;
  e.preventDefault();
  const keys = SETTINGS_TABS.map(t => t[0]);
  const step = ['ArrowDown', 'ArrowRight'].includes(e.key) ? 1 : -1;
  settingsTab = keys[(keys.indexOf(tab.dataset.id) + step + keys.length) % keys.length];
  renderSettingsPanel();
  if (settingsTab === 'mcp') loadMCP();
  if (settingsTab === 'logs') loadLogs();
  $('tab-' + settingsTab).focus();
});

// Deep link from the MCP/CLI "open panel" command: #item=<id> scrolls to and highlights that item.
function openFromHash() {
  const found = /[#&]item=([^&]+)/.exec(location.hash);
  if (found && state.items) focusItem(decodeURIComponent(found[1]));
}
window.addEventListener('hashchange', openFromHash);
refresh().then(openFromHash);
setInterval(refresh, 1800);
