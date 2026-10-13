import type { RepairPlan } from './api';
import { Badge, readable } from './ui';

export default function NativeRepairReview({review,expired=false,deadline,nativeState}:{review:NonNullable<RepairPlan['native_review']>;expired?:boolean;deadline?:number|null;nativeState?:string}) {
  const expiryDate=new Date(review.expires_utc_ms);
  const expiry=Number.isFinite(expiryDate.getTime())?expiryDate.toISOString():'Unavailable';
  const deadlineDate=typeof deadline==='number'?new Date(deadline):null;
  const deadlineText=deadlineDate&&Number.isFinite(deadlineDate.getTime())?deadlineDate.toISOString():null;
  return <section className="native-repair-review">
    <div className="line-between"><h4>Administrator-enrolled helper plan</h4><Badge tone="warn">Experimental · not qualified</Badge></div>
    <p>This helper plan authorizes only the displayed candidate and observed host state. Editing a source, opting out, or preparing a new plan requires a new review.</p>
    <dl className="metadata"><dt>Helper plan ID</dt><dd className="mono">{review.plan_id}</dd><dt>Authority</dt><dd>{review.authority}</dd><dt>Review expires (UTC)</dt><dd>{expiry}{expired?' · Expired':''}</dd><dt>Temporary rollback interval</dt><dd>{review.rollback_after_ms/1000} seconds</dd><dt>Observed state fingerprint</dt><dd className="mono">{review.before_fingerprint}</dd><dt>Reviewed plan fingerprint</dt><dd className="mono">{review.review_fingerprint}</dd></dl>
    <details className="technical" open><summary>Exact typed host change</summary><pre>{JSON.stringify(review.candidate,null,2)}</pre></details>
    {(deadlineText||nativeState)&&<dl className="metadata"><dt>Latest native receipt state</dt><dd>{nativeState?readable(nativeState):'Unavailable'}</dd><dt>Recorded rollback deadline (UTC)</dt><dd>{deadlineText||'Unavailable · inspect native recovery'}</dd></dl>}
    <p>Temporary repair: the helper will attempt exact rollback at its deadline only while fresh disarmed evidence and unchanged ownership permit it. A blocked rollback remains unresolved. The timer starts when apply intent is recorded, before the native write.</p>
    <p className="field-help">The helper rechecks identity, current safety evidence, host ownership, and plan validity before any change. Enrollment and opt-in do not establish production or hardware qualification. Disabling this engine session does not cancel the helper’s existing supervision.</p>
  </section>;
}
