import {migration, datasets as datasetsApi} from '../api/entities.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {MigrationStatus} from '../constants/enums.js';
import {esc, date, notify} from '../utils/dom.js';

const isAdmin = () => session.user.role === 'admin';

function progressBar(current, total) {
 const pct = total ? Math.round((current / total) * 100) : 0;
 return '<div class="progress"><div class="progress-fill" style="width:' + pct + '%"></div><span>' + pct + '%</span></div>';
}

export async function migrationPage() {
 const [state, list] = await Promise.all([migration.status(), datasetsApi.list()]);
 const counts = state.batch_counts || {};
 const canRun = isAdmin() && (state.status === 'idle' || state.status === 'paused' || state.status === 'failed');
 const canPause = isAdmin() && (state.status === 'running' || state.status === 'verifying');
 const verify = state.verification || {};
 return heading('06 / MIGRATION', '温标统一迁移与原值核对',
   '旧摄氏数据在线迁移到「原值 + 温标 + 摄氏分析值」统一存储。可按批暂停、中断恢复、重试不重复换算；完成前全量核对历史与同期新批次，漏迁不报完成。') +
  '<div class="migration-grid"><section class="panel"><div class="section-title"><h2>迁移控制台</h2>' +
  '<span class="status ' + esc(state.status) + '">' + esc(MigrationStatus[state.status] || state.status) + '</span></div>' +
  '<div class="migration-controls">' +
  '<label>批大小（每批记录数，50–2000）<input type="number" id="batch-size" min="50" max="2000" value="' + esc(state.batch_size || 200) + '" ' + (isAdmin() ? '' : 'disabled') + '></label>' +
  '<div class="button-row">' +
  '<button class="primary" id="mig-start" ' + (canRun ? '' : 'disabled') + '>' + (state.status === 'paused' ? '恢复迁移' : '启动迁移') + '</button>' +
  '<button id="mig-pause" ' + (canPause ? '' : 'disabled') + '暂停迁移</button>' +
  '<button id="mig-verify" ' + (isAdmin() ? '' : 'disabled') + '立即核对</button>' +
  '</div></div>' +
  (isAdmin() ? '' : '<p class="hint">仅业务负责人可启停迁移；当前账号只读。</p>') +
  '<div class="migration-metrics">' +
  [['目标数据集', state.total_datasets], ['目标记录', state.total_records], ['已迁移记录', state.migrated_records],
   ['失败记录', state.failed_records], ['待处理批', counts.pending || 0], ['进行中批', counts.running || 0],
   ['已完成批', counts.done || 0], ['失败批', counts.failed || 0]]
   .map(([k, v]) => '<div><span>' + esc(k) + '</span><strong>' + esc(v) + '</strong></div>').join('') +
  '</div>' + progressBar(state.migrated_records, state.total_records) +
  '<dl class="migration-meta">' +
  '<dt>协调者租约</dt><dd>' + esc(state.leader_id || '—') + (state.leader_heartbeat ? ' · 心跳 ' + date(state.leader_heartbeat) : '') + '</dd>' +
  '<dt>状态版本</dt><dd>v' + esc(state.version) + '（并发接管的乐观锁）</dd>' +
  '<dt>启动 / 更新 / 完成</dt><dd>' + (state.started_at ? date(state.started_at) : '—') + ' / ' +
   (state.updated_at ? date(state.updated_at) : '—') + ' / ' + (state.completed_at ? date(state.completed_at) : '—') + '</dd>' +
  (state.last_error ? '<dt>最近错误</dt><dd class="error-text">' + esc(state.last_error) + '</dd>' : '') +
  '</dl></section>' +

  '<section class="panel"><div class="section-title"><h2>完成前核对</h2><span>' +
  (verify.checked_at ? date(verify.checked_at) : '尚未核对') + '</span></div>' +
  (Object.keys(verify).length ? ('<div class="verify-card ' + (verify.passed ? 'pass' : 'fail') + '">' +
   '<strong>' + (verify.passed ? '核对通过：无漏迁' : '核对未通过：存在漏迁，不能报完成') + '</strong>' +
   '<p>已核对数据集 ' + esc(verify.datasets_checked) + ' 个、记录 ' + esc(verify.records_checked) + ' 条；' +
   '未完成批次 ' + esc((verify.open_batches || []).length) + ' 个。</p>' +
   ((verify.missing || []).length ? '<ul class="verify-missing">' + verify.missing.map(m =>
     '<li>' + esc(m.dataset_name) + '：' + esc(m.legacy_rows) + ' 行旧存储</li>').join('') + '</ul>' : '') +
   '</div>') : '<p class="empty">迁移跑完后会自动核对；也可手动触发立即核对。</p>') +
  '<div class="section-title" style="margin-top:24px"><h2>数据集迁移进度与原值</h2><span>' + list.length + ' 批</span></div>' +
  '<div class="dataset-trace">' + list.map(d =>
   '<article><div><strong>' + esc(d.name) + '</strong><p>' + esc(d.rows) + ' 条 · 存储形态 ' + esc(d.storage_generation) + '</p>' +
   '<small>显式温标 ' + esc(d.explicit_scale_rows) + ' · 已归一 ' + esc(d.normalized_rows ?? 0) +
   ' · 待迁移裸行 ' + esc(d.legacy_celsius_rows) + '</small></div>' +
   '<button class="text-button" data-trace="' + d.id + '">查看原值与温标 ↗</button></article>').join('') +
  '</div>' +
  ((state.open_batches || []).length ? '<div class="section-title" style="margin-top:24px"><h2>未完成 / 失败批次</h2><span>达到 ' + esc(state.max_batch_attempts || 5) + ' 次重试后停放</span></div><div class="open-batches">' +
   state.open_batches.map(b => '<article><div><strong>#' + esc(b.id) + ' · ' + esc(b.dataset_name) + '</strong><p>' + esc(b.status) + ' · 已尝试 ' + esc(b.attempts) + ' 次 · 已迁 ' + esc(b.migrated_rows) + ' 行</p>' +
    (b.error ? '<small class="error-text">' + esc(b.error) + '</small>' : '') + '</div>' +
    (b.status === 'failed' && isAdmin() ? '<button class="button small" data-reset="' + b.id + '">修数后重置</button>' : '') + '</article>').join('') + '</div>' : '') +
  '</section></div>' +
  '<section class="panel" id="trace-panel" hidden><div class="section-title"><h2 id="trace-title">原值与换算依据</h2><span>前 50 条</span></div><div id="trace-body"></div></section>';
}

function traceTable(records) {
 const cols = [
  {key: 'sample', label: '读数编号'}, {key: 'band', label: '温区'},
  {key: 'ref_orig', label: '标准原值'}, {key: 'ref_scale', label: '标准温标'},
  {key: 'ref_c', label: '标准摄氏 °C'},
  {key: 'meas_orig', label: '探头原值'}, {key: 'meas_scale', label: '探头温标'},
  {key: 'meas_c', label: '探头摄氏 °C'},
 ];
 const rows = records.slice(0, 50).map(r => {
  const c = r.conversion || {};
  return {
   sample: r.sample, band: r.band,
   ref_orig: c.reference ? c.reference.original_value : r.reference,
   ref_scale: c.reference ? c.reference.original_scale : 'CELSIUS',
   ref_c: r.reference,
   meas_orig: c.measured ? c.measured.original_value : r.measured,
   meas_scale: c.measured ? c.measured.original_scale : 'CELSIUS',
   meas_c: r.measured,
  };
 });
 return '<div class="table-wrap"><table><thead><tr>' + cols.map(c => '<th>' + esc(c.label) + '</th>').join('') +
  '</tr></thead><tbody>' + rows.map(r => '<tr>' + cols.map(c => '<td>' + esc(r[c.key]) + '</td>').join('') + '</tr>').join('') +
  '</tbody></table></div>';
}

export function bindMigration(refresh) {
 const click = (sel, fn) => document.querySelector(sel)?.addEventListener('click', fn);
 click('#mig-start', async () => {
  try {
   const size = Number(document.querySelector('#batch-size').value) || 200;
   const state = await migration.status();
   if (state.status === 'paused') await migration.resume(); else await migration.start(size);
   notify('迁移已启动，后台按批推进'); await refresh();
  } catch (e) { notify(e.message, true); }
 });
 click('#mig-pause', async () => { try { await migration.pause(); notify('已暂停：当前批次走完后不再领取新批次'); await refresh(); } catch (e) { notify(e.message, true); } });
 click('#mig-verify', async () => {
  try { const r = await migration.verify(); notify(r.passed ? '核对通过：无漏迁' : '核对未通过：仍有漏迁'); await refresh(); }
  catch (e) { notify(e.message, true); }
 });
 document.querySelectorAll('[data-trace]').forEach(btn => btn.addEventListener('click', async () => {
  try {
   const d = await datasetsApi.get(btn.dataset.trace);
   document.querySelector('#trace-panel').hidden = false;
   document.querySelector('#trace-title').textContent = d.name + ' · 原值与换算依据';
   document.querySelector('#trace-body').innerHTML = traceTable(d.records);
   document.querySelector('#trace-panel').scrollIntoView({behavior: 'smooth'});
  } catch (e) { notify(e.message, true); }
 }));
 document.querySelectorAll('[data-reset]').forEach(btn => btn.addEventListener('click', async () => {
  try {
   await migration.resetBatch(btn.dataset.reset);
   notify('批次已重置，迁移恢复后将重新处理（已迁行幂等跳过）'); await refresh();
  } catch (e) { notify(e.message, true); }
 }));
}
