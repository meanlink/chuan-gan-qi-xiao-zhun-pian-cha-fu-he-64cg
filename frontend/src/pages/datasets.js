import {datasets} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {esc,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {parseCSV} from '../utils/csv.js';
import {TemperatureScale} from '../constants/enums.js';

// 预览列：在业务列之外补充原值与温标（若记录已归一）。
function previewColumns(records){
 const m=workspace.meta;
 const cols=[...m.columns];
 if(records.some(r=>r&&r.conversion)){
  cols.push({key:'_ref_origin',label:'标准原值/温标'},{key:'_meas_origin',label:'探头原值/温标'});
 }
 return cols;
}
function previewRows(records){
 return records.slice(0,30).map(r=>{
  if(!r.conversion)return r;
  const c=r.conversion;
  return {...r,
   _ref_origin:`${c.reference.original_value} ${c.reference.original_scale}`,
   _meas_origin:`${c.measured.original_value} ${c.measured.original_scale}`};
 });
}
export async function dataPage(){
 workspace.datasets=await datasets.list();let selected=workspace.datasets[0]?await datasets.get(workspace.datasets[0].id):null,m=workspace.meta;
 const scaleOptions=Object.entries(TemperatureScale).map(([v,l])=>'<option value="'+v+'">'+esc(l)+'</option>').join('');
 return heading('02 / DATA INTAKE','数据接入与批次追溯','保留原始读数、温标与换算依据；分析统一使用摄氏。旧接入按摄氏写入，新接入逐条声明温标，未知温标或低于绝对零度将被拒绝。')+'<div class="dataset-grid"><section class="panel"><div class="section-title"><h2>业务批次</h2><span>'+workspace.datasets.length+' 批</span></div><div class="batch-list">'+workspace.datasets.map(d=>'<button class="batch-item" data-dataset="'+d.id+'"><strong>'+esc(d.name)+'</strong><span>'+d.rows+' 条 · '+esc(d.source)+'</span><small>'+esc(d.storage_generation)+' · 显式温标 '+d.explicit_scale_rows+' · 待迁移 '+d.legacy_celsius_rows+'</small><small>SHA256 '+esc(d.checksum.slice(0,18))+'…</small></button>').join('')+'</div><div class="section-title"><h2>接入新数据</h2></div><form id="dataset-form"><label>批次名称<input name="name" required minlength="2" placeholder="例如：11 月复测批次"></label><label>温标声明方式<select id="scale-mode"><option value="legacy">旧接入：不声明温标，按摄氏写入</option><option value="row">新接入：整批统一温标（行级 scale）</option><option value="field">新接入：逐条/逐字段温标（记录内带 scale 字段）</option></select></label><label>整批温标（统一模式生效）<select id="row-scale" disabled>'+scaleOptions+'</select></label><label>上传 CSV 或 JSON<input type="file" id="file-input" accept=".json,.csv"></label><label>记录 JSON<textarea name="records" rows="6" placeholder="粘贴 JSON 数组，或先选择上方文件" required></textarea></label><p class="hint">字段：'+m.columns.map(c=>esc(c.key)+' / '+esc(c.label)).join('，')+'。5–5,000 条。新接入支持 reference_scale/measured_scale 或行级 scale（Celsius/Fahrenheit/Kelvin，亦接受 C/F/K、°C/°F、摄氏/华氏/开尔文）。</p><button type="submit" class="primary" '+(session.user.role==='viewer'?'disabled':'')+'>校验并接入批次</button></form></section><section class="panel"><div class="section-title"><h2 id="preview-name">'+esc(selected?.name||'尚无批次')+'</h2><span>预览前 30 条</span></div><div id="data-preview">'+(selected?table(previewColumns(selected.records),previewRows(selected.records)):'')+'</div></section></div>';
}
export function bindData(refresh){
 let source='manual.json';
 const scaleMode=document.querySelector('#scale-mode');
 const rowScale=document.querySelector('#row-scale');
 scaleMode.addEventListener('change',()=>{rowScale.disabled=scaleMode.value!=='row';});
 document.querySelector('#file-input').addEventListener('change',async e=>{try{const file=e.target.files[0];if(!file)return;source=file.name;const text=await file.text(),records=file.name.endsWith('.csv')?parseCSV(text):JSON.parse(text);document.querySelector('[name=records]').value=JSON.stringify(records,null,2);}catch(error){notify(error.message,true);}});
 document.querySelectorAll('[data-dataset]').forEach(button=>button.onclick=async()=>{try{const d=await datasets.get(button.dataset.dataset);document.querySelector('#preview-name').textContent=d.name;document.querySelector('#data-preview').innerHTML=table(previewColumns(d.records),previewRows(d.records));}catch(error){notify(error.message,true);}});
 submit(document.querySelector('#dataset-form'),async f=>{
  let records=JSON.parse(f.get('records'));
  const mode=scaleMode.value;
  if(mode==='row'){const sc=rowScale.value;records=records.map(r=>({...r,scale:sc}));}
  // field 模式要求记录自身已带 scale / reference_scale / measured_scale，原样提交由后端校验
  await datasets.create({name:f.get('name'),source,records});
  notify('数据已接入：原值与温标已保留，分析统一按摄氏');await refresh();
 });
}
