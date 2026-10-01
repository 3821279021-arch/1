const el = id => document.getElementById(id);
const dimensions = {experiment: '实验', model: '模型', provider: 'Provider', role: '角色', faction: '阵营', seat: '座位', ruleset: '板子', date: '日期', status: '状态'};
let datasets = [];
const rows = () => datasets.flatMap(data => data.observations.map(row => ({...row, experiment: data.experiment_id})));
const percent = value => value == null ? '—' : `${(value * 100).toFixed(1)}%`;
const number = value => value == null ? '未知' : Number(value).toLocaleString(undefined, {maximumFractionDigits: 6});
function cell(parent, text) {const node = document.createElement('td'); node.textContent = String(text ?? '—'); parent.append(node);}
function wilson(wins, n) {
  if (!n) return [null, null];
  const z=1.959963984540054, p=wins/n, den=1+z*z/n, mid=(p+z*z/(2*n))/den, half=z*Math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den;
  return [Math.max(0,mid-half),Math.min(1,mid+half)];
}
function selected() {return Object.fromEntries(Object.keys(dimensions).map(dim=>[dim, el(`filter-${dim}`).value]));}
function filters() {
  el('filters').replaceChildren();
  const all = rows();
  for (const [dim, title] of Object.entries(dimensions)) {
    const label = document.createElement('label'); label.textContent = title;
    const select = document.createElement('select'); select.id=`filter-${dim}`;
    const opt = document.createElement('option'); opt.value=''; opt.textContent='全部'; select.append(opt);
    const values = dim==='status' ? datasets.flatMap(d=>Object.keys(d.status_counts)) : all.map(row=>String(row[dim]));
    for (const value of [...new Set(values)].sort()) {const option=document.createElement('option');option.value=value;option.textContent=value;select.append(option);}
    select.addEventListener('change', render);label.append(select);el('filters').append(label);
  }
}
function distribution(items, field) {const counts={};for(const row of items) counts[row[field]]=(counts[row[field]]||0)+1;return Object.entries(counts).map(([key,n])=>`${key}: ${n}`).join(' / ');}
function render() {
  const choice=selected();
  const active=datasets.filter(data=>!choice.experiment||data.experiment_id===choice.experiment);
  const filtered=rows().filter(row=>Object.entries(choice).every(([dim,value])=>!value||String(row[dim])===value));
  const wins=filtered.filter(row=>row.won).length, ci=wilson(wins,filtered.length);
  const completed=active.reduce((n,d)=>n+d.completed_games,0), planned=active.reduce((n,d)=>n+d.planned_games,0);
  const cards=[['玩家胜率',percent(filtered.length?wins/filtered.length:null),`N=${filtered.length}; 95% CI ${percent(ci[0])}–${percent(ci[1])}`],['已完成对局',`${completed} / ${planned}`,`实验 N=${active.length}`]];
  if (active.length===1) {
    const r=active[0].resources;
    cards.push(['模型调用',number(r.n),`Fallback ${percent(active[0].fallback.rate)} · N=${active[0].fallback.n}`],['P50 / P95 延迟',`${number(r.latency_p50_ms)} / ${number(r.latency_p95_ms)} ms`,`有效样本 N=${r.latency_n}`],['成本估算',number(r.estimated_cost),`价格版本 ${r.price_table_version}; N=${r.cost_n}`],['Token',`${number(r.input_tokens)} / ${number(r.output_tokens)}`,`输入 / 输出; usage N=${r.input_usage_n}/${r.output_usage_n}`]);
  }
  el('cards').replaceChildren();
  for(const [title,value,note] of cards){const card=document.createElement('div');card.className='card';const heading=document.createElement('span');heading.textContent=title;const strong=document.createElement('strong');strong.textContent=value;const small=document.createElement('small');small.textContent=note;card.append(heading,strong,small);el('cards').append(card);}
  el('versions').textContent=active.map(d=>`${d.experiment_id}: App ${d.manifest.app_version}; Rules ${d.manifest.ruleset_version}; Prompt ${d.manifest.prompt_version}; ${d.manifest.created_at}`).join(' | ');
  const groups=new Map();
  for(const row of filtered){const key=`${row.agent_id} / ${row.provider} / ${row.model}`;if(!groups.has(key))groups.set(key,[]);groups.get(key).push(row);}
  el('models').replaceChildren();
  for(const [key,items] of groups){const wins=items.filter(r=>r.won).length,[low,high]=wilson(wins,items.length);const tr=document.createElement('tr');const known=items.every(r=>r.cost!=null);for(const value of [key,`${wins} / ${items.length}`,percent(wins/items.length),`${percent(low)}–${percent(high)}`,distribution(items,'faction'),distribution(items,'role'),distribution(items,'seat'),`${items.reduce((n,r)=>n+r.fallbacks,0)} / ${items.reduce((n,r)=>n+r.calls,0)}`,number(known?items.reduce((n,r)=>n+r.cost,0):null)])cell(tr,value);el('models').append(tr);}
  el('games').replaceChildren();
  for(const data of active){if(choice.ruleset&&choice.ruleset!==data.manifest.ruleset.mode||choice.date&&choice.date!==data.manifest.created_at.slice(0,10))continue;for(const game of data.game_records||[]){if(choice.status&&game.status!==choice.status)continue;const tr=document.createElement('tr');for(const value of [data.experiment_id,game.game_id,game.game_seed,game.status,game.winner,game.days,game.error])cell(tr,value);el('games').append(tr);}}
}
el('files').addEventListener('change', async event => {
  try {
    const loaded=[];
    for(const file of event.target.files){if(file.size>30*1024*1024)throw new Error('单个文件不得超过 30 MB');
      // Preserve integer seed lexemes before JSON converts uint64 to a double.
      const raw=await file.text();
      const data=JSON.parse(raw.replace(/("game_seed"\s*:\s*)(-?\d+)(?=\s*[,}\]])/g, '$1"$2"'));
      if(data.schema_version!=='1.0.0'||!Array.isArray(data.observations)||!data.manifest||!data.resources||!data.status_counts)throw new Error('需要 V3.1 summary.json');loaded.push(data);}
    datasets=loaded;filters();render();el('message').textContent=`已导入 ${datasets.length} 个实验。资源卡显示整个实验的统计；玩家表按筛选条件统计。`;
  } catch (error) {el('message').textContent=`导入失败：${error.message}`;}
});
