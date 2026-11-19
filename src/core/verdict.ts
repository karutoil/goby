import {createHash} from 'node:crypto';
import {gateAllowed, type GateFacts} from './rules.ts';
import type {VerifyMode} from './verify.ts';

/**
 * What a review leaves behind for the gate to re-read.
 *
 * Schema 3 carries the verification verdict: which posted findings the harness reproduced, and how
 * many it withheld because it could not. `parseReceipt` accepts one exact key set, so a schema-2
 * receipt is not misread as a schema-3 one — it is no receipt, and the head needs a new review.
 */
export interface ReviewReceipt {
  schema: 3;
  head_sha: string;
  base_ref: string;
  diff_sha256: string;
  complete: boolean;
  agent: boolean;
  unthreaded: number;
  finding_ids: string[];
  /** The verification mode this review ran under, so a gate can refuse a weaker review. */
  verify: VerifyMode;
  /** Posted findings the verifier reproduced. A subset of `finding_ids` in `advisory` mode. */
  verified_ids: string[];
  /** Findings kept out of the review because they could not be reproduced. */
  withheld: number;
  /** Of those, how many were high severity: the ones a gate may not wave through. */
  withheld_high: number;
  /** Findings posted without a reproduction, in `advisory` mode. Informational, never gated. */
  unverified: number;
}
export interface GateResult {head_sha: string; state: 'success' | 'failure' | 'error'; description: string}
export interface GateThread {fid?: string; sha?: string}
export interface GatePolicy {verify: VerifyMode}
const PREFIX = '<!-- goby-gate ';
const RECEIPT_KEYS = 'agent,base_ref,complete,diff_sha256,finding_ids,head_sha,schema,unthreaded,unverified,verified_ids,verify,withheld,withheld_high';
const MODES = new Set(['off', 'advisory', 'required']);
export const validSha = (value: unknown): value is string => typeof value === 'string' && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value);
export function diffDigest(diff: string): string { return createHash('sha256').update(diff).digest('hex'); }
export function receiptMarker(receipt: ReviewReceipt): string { return `${PREFIX}${JSON.stringify(receipt)} -->`; }
const nonNegative = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const ids = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(id => typeof id === 'string' && /^[0-9a-f]+$/.test(id)) && new Set(value).size === value.length;

export function parseReceipt(body: string, commit: string): ReviewReceipt | null {
  const lines = body.split(/\r?\n/).filter(line => line.startsWith(PREFIX));
  if (lines.length !== 1 || !lines[0].endsWith(' -->')) return null;
  try {
    const data: unknown = JSON.parse(lines[0].slice(PREFIX.length, -4));
    if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
    const r = data as Record<string, unknown>;
    if (Object.keys(r).sort().join(',') !== RECEIPT_KEYS) return null;
    if (r.schema !== 3 || typeof r.base_ref !== 'string' || !r.base_ref || typeof r.diff_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(r.diff_sha256) || !validSha(r.head_sha) || r.head_sha !== commit || typeof r.complete !== 'boolean' || typeof r.agent !== 'boolean') return null;
    if (!nonNegative(r.unthreaded) || !ids(r.finding_ids) || !ids(r.verified_ids)) return null;
    if (typeof r.verify !== 'string' || !MODES.has(r.verify)) return null;
    if (!nonNegative(r.withheld) || !nonNegative(r.withheld_high) || !nonNegative(r.unverified)) return null;
    return r as unknown as ReviewReceipt;
  } catch { return null; }
}
export function evaluateGate(receipt: ReviewReceipt | null, head: string, opened: GateThread[], resolved: GateThread[], complete: boolean, executionSucceeded: boolean, evidence?: {base_ref: string; diff_sha256: string}, policy: GatePolicy = {verify: 'off'}): GateResult {
  const observed = new Set([...opened, ...resolved].filter(t => t.sha === head.slice(0, 10) || t.sha === head).map(t => t.fid));
  const verified = new Set(receipt?.verified_ids ?? []);
  const required = policy.verify === 'required';
  const facts: GateFacts = {
    receipt_valid: receipt !== null,
    same_head: receipt?.head_sha === head,
    complete: receipt?.complete === true,
    agent: receipt?.agent === true,
    no_unthreaded: receipt?.unthreaded === 0,
    threads_complete: complete,
    findings_accounted: receipt !== null && receipt.finding_ids.every(id => observed.has(id)),
    no_open_threads: opened.length === 0,
    execution_succeeded: executionSucceeded,
    diff_matches: !!receipt && !!evidence && receipt.base_ref === evidence.base_ref && receipt.diff_sha256 === evidence.diff_sha256,
    // A review run with verification off cannot satisfy a gate that requires it: the mode is part
    // of the evidence, not an input to the review.
    verify_policy: receipt !== null && receipt.verify === policy.verify,
    // In `required` mode every posted finding has to be one the harness reproduced.
    verification_valid: receipt !== null && (!required || receipt.finding_ids.every(id => verified.has(id))),
    // A high-severity claim nobody could reproduce is not a green merge; it is a human decision.
    verified_only: receipt === null ? false : (!required || receipt.withheld_high === 0),
  };
  const reasons: Record<keyof GateFacts, string> = {
    receipt_valid: 'No valid review receipt for this head', same_head: 'Review belongs to a different commit',
    complete: 'Review incomplete (budget exhausted or answer truncated)', agent: 'An agentic review is required',
    no_unthreaded: 'Unthreaded or capped findings require a new review after a fix',
    threads_complete: 'Thread state is incomplete; refusing to guess', findings_accounted: 'Published finding threads are missing',
    no_open_threads: `${opened.length} unresolved Goby finding(s)`, execution_succeeded: 'Latest review execution did not succeed',
    diff_matches: 'PR base or diff changed since this review; a new review is required',
    verify_policy: `This review ran verification '${receipt?.verify ?? 'none'}' but the gate requires '${policy.verify}'; a new review is required`,
    verification_valid: 'A posted finding has no reproduction in this review',
    verified_only: `${receipt?.withheld_high ?? 0} high-severity claim(s) could not be reproduced; human review required`,
  };
  if (gateAllowed(facts)) return {head_sha: head, state: 'success', description: 'Complete review; no unresolved Goby findings'};
  const key = (Object.keys(facts) as (keyof GateFacts)[]).find(key => facts[key] !== true)!;
  return {head_sha: head, state: 'failure', description: reasons[key]};
}
