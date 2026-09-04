const el = {
  serviceState: document.getElementById('serviceState'), filePicker: document.getElementById('filePicker'),
  fileInput: document.getElementById('fileInput'), fileSection: document.getElementById('fileSection'),
  fileCount: document.getElementById('fileCount'), fileList: document.getElementById('fileList'),
  addMoreButton: document.getElementById('addMoreButton'), clearButton: document.getElementById('clearButton'),
  startButton: document.getElementById('startButton'), resultCard: document.getElementById('resultCard'),
  resultSummary: document.getElementById('resultSummary'), resultList: document.getElementById('resultList'),
  exportAllButton: document.getElementById('exportAllButton'), toast: document.getElementById('toast')
};

const fileSvg = '<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6zM14 3v5h5M9 13h6M9 17h4"/></svg>';
const { safeName, buildMerged } = DocumentUtils;
let jobs = [];
let running = false;
let stopRequested = false;
let activeController = null;
let toastTimer = null;

function idFor(file) { return `${file.name}-${file.size}-${file.lastModified}`; }
function formatSize(bytes) { const units=['B','KB','MB','GB']; let value=bytes,index=0; while(value>=1024&&index<3){value/=1024;index+=1;} return `${value.toFixed(index>1?1:0)} ${units[index]}`; }
function formatDuration(seconds) { const total=Math.round(seconds||0); const h=Math.floor(total/3600),m=Math.floor((total%3600)/60),s=total%60; return h?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${m}:${String(s).padStart(2,'0')}`; }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g,(char)=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char])); }

async function checkService() {
  try {
    const response=await fetch('/api/health',{cache:'no-store'}); if(!response.ok) throw new Error();
    const data=await response.json(); if(!data.model_ready) throw new Error();
    el.serviceState.className='local-note ready';
    el.serviceState.innerHTML=`<span class="status-dot"></span><span>本地模型已就绪 · ${escapeHtml(data.device.toUpperCase())}</span>`;
  } catch (_) {
    el.serviceState.className='local-note error'; el.serviceState.innerHTML='<span class="status-dot"></span><span>本地服务未启动</span>';
  }
}

function addFiles(fileList) {
  [...fileList].forEach((file)=>{ if(!jobs.some((job)=>job.id===idFor(file))) jobs.push({id:idFor(file),file,status:'pending',text:'',result:null,reviewed:false,expanded:false,error:''}); });
  renderFiles(); renderResults();
}

function renderFiles() {
  el.filePicker.hidden=jobs.length>0; el.fileSection.hidden=jobs.length===0; el.clearButton.hidden=jobs.length===0;
  el.fileCount.textContent=`已选择 ${jobs.length} 个文件`;
  el.startButton.disabled=jobs.length===0||(!running&&!jobs.some((job)=>job.status==='pending'||job.status==='error'));
  el.startButton.textContent=running?'停止处理':'开始本地转写';
  const labels={pending:'等待处理',processing:'本地转写中',done:'已完成',error:'处理失败',stopped:'已停止'};
  el.fileList.innerHTML=jobs.map((job)=>`<div class="file-row"><span class="file-icon">${fileSvg}</span><div class="file-info"><strong>${escapeHtml(job.file.name)}</strong><small>${formatSize(job.file.size)}</small></div><span class="file-status ${job.status}">${job.status==='processing'?'<i class="spinner"></i>':''}${labels[job.status]}</span><button class="remove-file" data-remove="${escapeHtml(job.id)}" ${running?'disabled':''}>移除</button></div>`).join('');
  el.fileList.querySelectorAll('[data-remove]').forEach((button)=>button.addEventListener('click',()=>{jobs=jobs.filter((job)=>job.id!==button.dataset.remove);renderFiles();renderResults();}));
}

function renderResults() {
  const visible=jobs.filter((job)=>job.status!=='pending'); el.resultCard.hidden=visible.length===0;
  const done=jobs.filter((job)=>job.status==='done').length, reviewed=jobs.filter((job)=>job.status==='done'&&job.reviewed).length;
  el.resultSummary.textContent=running?`正在处理 ${done+1} / ${jobs.length}`:`已完成 ${done} / ${jobs.length}，已复核 ${reviewed} 份`;
  el.exportAllButton.disabled=done===0||reviewed!==done||running;
  el.resultList.innerHTML=visible.map((job)=>{
    if(job.status==='processing') return `<article class="result-item"><div class="result-item-head"><div class="result-file"><strong>${escapeHtml(job.file.name)}</strong><small>本地模型正在识别，长文件需要一些时间</small></div></div><div class="pending-box"><i class="spinner"></i>正在解码音视频并转写中文…</div></article>`;
    if(job.status==='error'||job.status==='stopped') return `<article class="result-item"><div class="result-item-head"><div class="result-file"><strong>${escapeHtml(job.file.name)}</strong><small>${job.status==='stopped'?'任务已停止':'没有生成文档'}</small></div><div class="result-actions"><button class="small-button" data-retry="${escapeHtml(job.id)}">重新处理</button></div></div><div class="pending-box error">${escapeHtml(job.error||'处理已停止')}</div></article>`;
    const result=job.result;
    return `<article class="result-item"><div class="result-item-head"><div class="result-file"><strong>${escapeHtml(job.file.name)}</strong><small>${formatDuration(result.duration)} · ${job.text.replace(/\s/g,'').length} 字 · 本地处理 ${result.processing_seconds} 秒</small></div><div class="result-actions"><button class="small-button" data-toggle="${escapeHtml(job.id)}">${job.expanded?'收起':'复核文字'}</button><button class="small-button export" data-export="${escapeHtml(job.id)}" ${job.reviewed?'':'disabled'}>单独导出</button></div></div>${job.expanded?`<div class="review-area"><div class="review-meta"><span>可直接修改识别结果</span><span class="${result.low_confidence_count?'warning':''}">${result.low_confidence_count?`${result.low_confidence_count} 个片段建议重点复核`:'模型未标低置信度，仍需人工通读'}</span></div><textarea data-text="${escapeHtml(job.id)}" spellcheck="false">${escapeHtml(job.text)}</textarea><label class="review-check"><input type="checkbox" data-review="${escapeHtml(job.id)}" ${job.reviewed?'checked':''}><span></span>我已复核这份文字</label></div>`:''}</article>`;
  }).join('');
  bindResultActions();
}

function bindResultActions() {
  el.resultList.querySelectorAll('[data-toggle]').forEach((button)=>button.addEventListener('click',()=>{const job=jobs.find((item)=>item.id===button.dataset.toggle);job.expanded=!job.expanded;renderResults();}));
  el.resultList.querySelectorAll('[data-text]').forEach((area)=>area.addEventListener('input',()=>{jobs.find((item)=>item.id===area.dataset.text).text=area.value;}));
  el.resultList.querySelectorAll('[data-review]').forEach((check)=>check.addEventListener('change',()=>{jobs.find((item)=>item.id===check.dataset.review).reviewed=check.checked;renderResults();}));
  el.resultList.querySelectorAll('[data-export]').forEach((button)=>button.addEventListener('click',()=>exportOne(jobs.find((item)=>item.id===button.dataset.export))));
  el.resultList.querySelectorAll('[data-retry]').forEach((button)=>button.addEventListener('click',()=>{const job=jobs.find((item)=>item.id===button.dataset.retry);job.status='pending';job.error='';startQueue();}));
}

async function transcribeJob(job) {
  job.status='processing'; job.expanded=false; renderFiles(); renderResults();
  activeController=new AbortController(); const body=new FormData(); body.append('file',job.file);
  try {
    const response=await fetch('/api/transcribe',{method:'POST',body,signal:activeController.signal});
    if(!response.ok){let message=`本地服务返回 ${response.status}`;try{const detail=await response.json();message=detail.detail||message;}catch(_){}throw new Error(message);}
    const result=await response.json(); if(!result.text?.trim()) throw new Error('没有识别到文字内容');
    job.status='done'; job.result=result; job.text=result.text; job.expanded=true;
  } catch(error) {
    job.status=error.name==='AbortError'?'stopped':'error'; job.error=error.name==='AbortError'?'用户停止了处理':error.message;
  } finally { activeController=null; renderFiles(); renderResults(); }
}

async function startQueue() {
  if(running){stopRequested=true;activeController?.abort();return;}
  running=true;stopRequested=false;renderFiles();
  for(const job of jobs){if(stopRequested)break;if(job.status==='pending'||job.status==='error')await transcribeJob(job);}
  running=false;renderFiles();renderResults();
  if(!stopRequested) showToast('本地转写任务已完成');
}

function download(name,text) { const blob=new Blob([text],{type:'text/plain;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=name;link.click();URL.revokeObjectURL(url); }
function exportOne(job) { if(!job?.reviewed)return;download(`${safeName(job.file.name)}.txt`,job.text.trim());showToast('已导出单份文档'); }
function exportAll() {
  const items=jobs.filter((job)=>job.status==='done'&&job.reviewed); if(!items.length)return;
  const text=buildMerged(items.map((job)=>({name:job.file.name,text:job.text})));
  download('全部转写结果.txt',text); showToast(`已合并导出 ${items.length} 份结果`);
}
function clearAll() { if(running)return;jobs=[];el.fileInput.value='';renderFiles();renderResults(); }
function showToast(message) { el.toast.textContent=message;el.toast.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.toast.classList.remove('show'),2400); }

el.filePicker.addEventListener('click',()=>el.fileInput.click()); el.addMoreButton.addEventListener('click',()=>el.fileInput.click());
el.fileInput.addEventListener('change',(event)=>{addFiles(event.target.files);event.target.value='';}); el.clearButton.addEventListener('click',clearAll);
el.startButton.addEventListener('click',startQueue); el.exportAllButton.addEventListener('click',exportAll);
['dragenter','dragover'].forEach((name)=>el.filePicker.addEventListener(name,(event)=>{event.preventDefault();el.filePicker.classList.add('dragging');}));
['dragleave','drop'].forEach((name)=>el.filePicker.addEventListener(name,(event)=>{event.preventDefault();el.filePicker.classList.remove('dragging');}));
el.filePicker.addEventListener('drop',(event)=>addFiles(event.dataTransfer.files));
checkService(); renderFiles();
