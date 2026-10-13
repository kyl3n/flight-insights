import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage, listAll, request, type EngineJob } from './api';

export const jobActive=(job:EngineJob)=>job.state==='queued'||job.state==='running';

// Job records belong to the engine, so navigation and renderer reload do not
// replay acquisition or analysis. Only explicitly queued operations start work.
export function useEngineJobs(ready:boolean,methods:readonly string[],onComplete:(jobs:EngineJob[])=>void){
  const [jobs,setJobs]=useState<EngineJob[]>([]);
  const [error,setError]=useState('');const [available,setAvailable]=useState(false);
  const [retry,setRetry]=useState(0);
  const seen=useRef(new Set<string>());const complete=useRef(onComplete);complete.current=onComplete;
  const methodKey=methods.join('|');
  useEffect(()=>{
    if(!ready){setAvailable(false);return;}
    let mounted=true;
    void listAll<EngineJob>('jobs.list','jobs').then(result=>{if(mounted){setJobs(result.filter(job=>methodKey.split('|').includes(job.method)));setAvailable(true);setError('');}}).catch(cause=>{if(mounted){setError(errorMessage(cause));setAvailable(false);}});
    return()=>{mounted=false;};
  },[ready,methodKey]);
  useEffect(()=>{
    if(!ready||!available||!jobs.some(jobActive))return;
    let mounted=true;
    const timer=setTimeout(async()=>{
      try{const updates=await Promise.all(jobs.filter(jobActive).map(job=>request<{job:EngineJob}>('jobs.get',{job_id:job.id})));if(mounted){setJobs(previous=>previous.map(job=>updates.find(item=>item.job.id===job.id)?.job||job));setError('');}}
      catch(cause){if(mounted){setError(errorMessage(cause));setRetry(value=>value+1);}}
    },750);
    return()=>{mounted=false;clearTimeout(timer);};
  },[jobs,ready,available,retry]);
  useEffect(()=>{const completed=jobs.filter(job=>job.state==='succeeded'&&!seen.current.has(job.id));if(completed.length){completed.forEach(job=>seen.current.add(job.id));complete.current(completed);}},[jobs]);
  const queue=useCallback((job:EngineJob)=>setJobs(previous=>[job,...previous.filter(item=>item.id!==job.id)]),[]);
  const cancel=useCallback(async(job:EngineJob)=>{const result=await request<{job:EngineJob}>('jobs.cancel',{job_id:job.id});queue(result.job);},[queue]);
  return {jobs,error,available,queue,cancel,pending:jobs.some(jobActive)};
}
