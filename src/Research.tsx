import { useEffect, useRef, useState, type FormEvent } from 'react';
import { errorMessage, request, type EngineJob, type Run } from './api';
import BackgroundJobs from './BackgroundJobs';
import { useEngineJobs } from './useEngineJobs';
import { Badge, Empty, Icon, Notes, readable } from './ui';

interface ResearchTopic {id:string;title:string;source_ids:string[]}
interface ResearchResult {
  topic:string;title:string;capability_requirements:string[];warnings:string[];limitations:string[];
  latest_check:null|{state:string;checked_at:string;revision:string;source_count:number;errors:unknown[]};
  results:{source_id:string;title:string;url:string;revision:string;sha256:string;retrieved_at:string;commit_at:string|null;
    review_state:string;cache_state:string;changed_since_bundled:boolean|null;
    excerpts:{text:string;line_start:number;line_end:number;truncated:boolean;basis:string}[]}[];
}

export default function Research({ready,busy,run,observerActive}:{ready:boolean;busy:string;run:Run;observerActive:boolean}) {
  const [topics,setTopics]=useState<ResearchTopic[]>([]);const [topic,setTopic]=useState('vibration');
  const [query,setQuery]=useState('');const [result,setResult]=useState<ResearchResult|null>(null);
  const [online,setOnline]=useState(false);const [error,setError]=useState('');
  const generation=useRef(0);const selected=useRef({topic,query});selected.current={topic,query};
  const jobs=useEngineJobs(ready,['research.refresh'],completed=>{
    if(completed.some(job=>(job.result as ResearchResult|undefined)?.topic===selected.current.topic))void loadQuery(selected.current.topic,selected.current.query).catch(cause=>setError(errorMessage(cause)));
  });
  async function loadQuery(nextTopic:string,nextQuery:string) {
    const version=++generation.current;
    const value=await request<ResearchResult>('research.query',{topic:nextTopic,...(nextQuery.trim()?{query:nextQuery.trim()}:{})});
    if(version===generation.current){setResult(value);setError('');}
  }
  useEffect(()=>{
    if(!ready)return;
    let current=true;
    void request<{topics:ResearchTopic[]}>('research.status').then(value=>{if(current)setTopics(value.topics);}).catch(cause=>{if(current)setError(errorMessage(cause));});
    void loadQuery(selected.current.topic,selected.current.query).catch(cause=>{if(current)setError(errorMessage(cause));});
    return()=>{current=false;generation.current++;};
  },[ready]);
  function chooseTopic(value:string){setTopic(value);setQuery('');setOnline(false);setResult(null);void run('Opening local research',()=>loadQuery(value,''));}
  function search(event:FormEvent){event.preventDefault();void run('Searching saved source passages',()=>loadQuery(topic,query));}
  async function updateSources(){if(!online||observerActive)return;await run('Queuing official source update',async()=>{const response=await request<{job:EngineJob}>('research.refresh',{topic,allow_online:true});jobs.queue(response.job);setOnline(false);});}
  return <div className="stack research-workspace"><div className="page-title"><div><span className="eyebrow">RESEARCH WITH TRACEABLE SOURCES</span><h1>Research library</h1><p>Investigate performance problems using official documentation, saved source passages, and explicit hardware requirements.</p></div><Badge>Offline first</Badge></div>
    <section className="panel padded"><form onSubmit={search}><div className="form-grid"><label>Performance research topic<select value={topic} disabled={!ready||Boolean(busy)} onChange={event=>chooseTopic(event.target.value)}>{topics.length?topics.map(item=><option key={item.id} value={item.id}>{item.title}</option>):<option value="vibration">Vibration</option>}</select></label><label>Search saved passages<input value={query} maxLength={160} placeholder="For example RPM, clipping, or bandwidth" onChange={event=>setQuery(event.target.value)}/></label></div><button className="button secondary" disabled={!ready||Boolean(busy)}>Search local library</button><p className="field-help">Search words stay on this computer. Bundled guidance and previously downloaded sources work without an internet connection.</p></form></section>
    <section className="panel"><div className="panel-heading"><div><h2>Check current official sources</h2><p>Explicitly download the latest available documentation for the selected topic.</p></div></div><div className="padded"><p>The update contacts official ArduPilot repositories on GitHub. It sends no flight logs, parameters, network addresses, software inventory, or search words. Downloaded changes are saved for review and do not automatically change tuning advice or earlier reports.</p><p className="field-help">Starting a recording cancels an unfinished update at its next cancellation check. Saved sources remain available.</p>{observerActive&&<Notes title="Recording is active" items={['Stop recording before updating sources. The saved research library remains available during recording.']}/>}<label className="checkbox-label"><input type="checkbox" checked={online} onChange={event=>setOnline(event.target.checked)} disabled={observerActive||jobs.pending}/>Use an internet connection to check official sources for this topic</label><button className="button secondary" disabled={!ready||Boolean(busy)||!jobs.available||jobs.pending||!online||observerActive} onClick={updateSources}><Icon name="refresh"/>Check official sources</button>{result?.latest_check&&<p className="field-help">Last saved check: {readable(result.latest_check.state)} · {result.latest_check.checked_at}</p>}</div></section>
    <BackgroundJobs jobs={jobs.jobs} ready={ready} busy={busy} run={run} cancel={jobs.cancel} labels={{'research.refresh':'Official research source update'}}/>
    {(error||jobs.error)&&<Notes title="Research needs attention" items={[error||jobs.error]}/>}
    {result&&<><Notes title="Evidence needed for this topic" items={result.capability_requirements}/><Notes title="Source update notes" items={result.warnings}/><section className="panel"><div className="panel-heading"><div><h2>{result.title}</h2><p>Source passages support engineering review; applicability depends on the installed firmware and hardware.</p></div></div><div className="padded research-sources">{result.results.length?result.results.map(source=><article className="research-source" key={`${source.source_id}-${source.revision}`}><div className="line-between"><h3>{source.title}</h3><Badge tone={source.review_state==='unreviewed_source'?'warn':'blue'}>{source.review_state==='unreviewed_source'?'New source · review required':'Bundled reviewed excerpt'}</Badge></div><p className="field-help">{source.cache_state==='cached'?'Saved on this computer':'Available offline'} · retrieved {source.retrieved_at} · revision {source.revision.slice(0,12)}</p>{source.changed_since_bundled===true&&<p>The source differs from the bundled guidance revision. Check the changes and your firmware before using it to plan a parameter change.</p>}{source.excerpts.length?source.excerpts.map((excerpt,index)=><blockquote className="research-excerpt" key={index}><p>{excerpt.text}</p><cite>Source lines {excerpt.line_start}–{excerpt.line_end}{excerpt.truncated?' · excerpt shortened':''}</cite></blockquote>):<p>No saved passage matched this search. Try a more specific term or clear the search.</p>}<a href={source.url} target="_blank" rel="noreferrer">Open this exact official source revision</a><details className="technical"><summary>Source integrity and retrieval</summary><dl className="metadata"><dt>Source SHA-256</dt><dd className="mono break-word">{source.sha256}</dd><dt>Revision</dt><dd className="mono break-word">{source.revision}</dd><dt>Source commit date</dt><dd>{source.commit_at||'Not recorded'}</dd></dl></details></article>):<Empty icon="evidence" title="No matching saved passages">Clear the search or choose another topic.</Empty>}</div></section><Notes title="Research coverage" items={result.limitations}/></>}
  </div>;
}
