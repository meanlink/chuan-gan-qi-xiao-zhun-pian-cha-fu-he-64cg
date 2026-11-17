import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const migration={
 status:()=>api('/migration/status'),
 start:batch_size=>api('/migration/start',{method:'POST',body:JSON.stringify({batch_size})}),
 pause:()=>api('/migration/pause',{method:'POST'}),
 resume:()=>api('/migration/resume',{method:'POST'}),
 verify:()=>api('/migration/verify',{method:'POST'}),
 batch:id=>api('/migration/batches/'+id),
 resetBatch:id=>api('/migration/batches/'+id+'/reset',{method:'POST'}),
};
