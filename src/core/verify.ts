/**
 * The verification verdict: what the harness re-ran, and what survived.
 *
 * `verify/verify.py` writes it; this module decides whether the file is evidence. The parse is
 * strict for the same reason the findings parse is: a verdict we cannot read must not degrade into
 * "nothing was withheld", which is the one answer that turns an unverified claim into a merge.
 *
 * The verdict is bound to the head and to the diff digest `prepare` computed from the API's diff,
 * so a verdict from an earlier push cannot be re-used for this one.
 */
import type { Finding } from './contract.ts';

export type VerifyMode = 'off' | 'advisory' | 'required';
export type VerifyStatus = 'verified' | 'withheld';

export interface VerifyRecord {
  path: string;
  line: number;
  title: string;
  severity: string;
  status: VerifyStatus;
  kind: string;
  detail: string;
}
export interface Verification {
  schema: 1;
  head_sha: string;
  diff_sha256: string;
  mode: 'required' | 'advisory';
  records: VerifyRecord[];
  degraded: string[];
}

const STATUSES = new Set(['verified', 'withheld']);
const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const trim = (value: unknown): string => (typeof value === 'string' ? value.trim() : '');

export function parseVerification(raw: string | null, head: string, digest: string): Verification | null {
  if (raw === null) return null;
  let data: unknown;
  try { data = JSON.parse(raw); } catch { return null; }
  if (!isRecord(data)) return null;
  if (data.schema !== 1 || data.head_sha !== head || data.diff_sha256 !== digest) return null;
  if (data.mode !== 'required' && data.mode !== 'advisory') return null;
  if (!Array.isArray(data.records) || !Array.isArray(data.degraded)) return null;
  if (!data.degraded.every(entry => typeof entry === 'string')) return null;

  const seen = new Set<string>();
  const records: VerifyRecord[] = [];
  for (const entry of data.records) {
    if (!isRecord(entry)) return null;
    const path = trim(entry.path);
    const title = trim(entry.title);
    const line = entry.line;
    const status = entry.status;
    if (!path || !title) return null;
    if (typeof line !== 'number' || !Number.isSafeInteger(line) || line < 1) return null;
    if (typeof status !== 'string' || !STATUSES.has(status)) return null;
    if (typeof entry.kind !== 'string' || !entry.kind) return null;
    if (typeof entry.detail !== 'string') return null;
    const key = findingKey({ path, line, title });
    // A verifier that emits two verdicts for one claim did not decide anything.
    if (seen.has(key)) return null;
    seen.add(key);
    records.push({
      path, line, title, status: status as VerifyStatus, kind: entry.kind,
      detail: entry.detail, severity: trim(entry.severity) || 'medium',
    });
  }
  return {
    schema: 1, head_sha: data.head_sha as string, diff_sha256: data.diff_sha256 as string,
    mode: data.mode, records, degraded: data.degraded as string[],
  };
}

/** The same triple `parse_findings` de-duplicates on, and the verifier keys its records by. */
export function findingKey(finding: Pick<Finding, 'path' | 'line' | 'title'>): string {
  return `${finding.path}\n${finding.line}\n${trim(finding.title).toLowerCase()}`;
}

export function indexVerification(verification: Verification): Map<string, VerifyRecord> {
  return new Map(verification.records.map(record => [findingKey(record), record]));
}

/**
 * Stamp every finding with the verdict, in place.
 *
 * A finding with no record is `unverified`, not "fine": the only way to be verified is for the
 * verifier to have said so about this exact claim.
 */
export function stampVerification(findings: readonly Finding[], verification: Verification | null): void {
  const index = verification === null ? new Map() : indexVerification(verification);
  for (const finding of findings) {
    finding.verification = index.get(findingKey(finding))?.status === 'verified' ? 'verified' : 'unverified';
  }
}

export function partition(findings: readonly Finding[]): [Finding[], Finding[]] {
  const verified: Finding[] = [], unverified: Finding[] = [];
  for (const finding of findings) (finding.verification === 'verified' ? verified : unverified).push(finding);
  return [verified, unverified];
}

