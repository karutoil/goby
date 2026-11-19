import { describe, expect, test } from 'bun:test';
import { Finding } from '../src/core/contract.ts';
import { indexVerification, parseVerification, partition, stampVerification, type Verification } from '../src/core/verify.ts';

const head = 'a'.repeat(40);
const digest = 'b'.repeat(64);

function verdict(overrides: Partial<Record<string, unknown>> = {}): string {
  return JSON.stringify({
    schema: 1,
    head_sha: head,
    diff_sha256: digest,
    mode: 'required',
    records: [{
      path: 'src/a.ts', line: 4, title: 'Off by one', severity: 'high',
      status: 'verified', kind: 'regression', detail: 'fails at head, passes at the merge base',
    }],
    degraded: [],
    ...overrides,
  });
}

describe('the verification verdict is only evidence when it is about this review', () => {
  test('a matching verdict parses', () => {
    const parsed = parseVerification(verdict(), head, digest);
    expect(parsed?.records).toHaveLength(1);
    expect(parsed?.records[0]!.status).toBe('verified');
  });

  test('a verdict for another head, another diff or another schema is not this review', () => {
    expect(parseVerification(verdict(), 'c'.repeat(40), digest)).toBeNull();
    expect(parseVerification(verdict(), head, 'd'.repeat(64))).toBeNull();
    expect(parseVerification(verdict({ schema: 2 }), head, digest)).toBeNull();
    expect(parseVerification(verdict({ mode: 'off' }), head, digest)).toBeNull();
    expect(parseVerification(null, head, digest)).toBeNull();
    expect(parseVerification('{}', head, digest)).toBeNull();
  });

  test('two verdicts for one claim did not decide anything', () => {
    const record = { path: 'a.ts', line: 1, title: 'x', status: 'verified', kind: 'regression', detail: '' };
    expect(parseVerification(verdict({ records: [record, record] }), head, digest)).toBeNull();
  });

  test('an unknown status or a missing detail is off-contract', () => {
    const record = { path: 'a.ts', line: 1, title: 'x', status: 'maybe', kind: 'regression', detail: '' };
    expect(parseVerification(verdict({ records: [record] }), head, digest)).toBeNull();
    const missing = { path: 'a.ts', line: 1, title: 'x', status: 'verified', kind: 'regression' };
    expect(parseVerification(verdict({ records: [missing] }), head, digest)).toBeNull();
  });
});

describe('stamping decides what a finding is worth', () => {
  const verification: Verification = {
    schema: 1, head_sha: head, diff_sha256: digest, mode: 'required',
    records: [{
      path: 'src/a.ts', line: 4, title: 'off by one', severity: 'high',
      status: 'verified', kind: 'regression', detail: 'reproduced',
    }],
    degraded: [],
  };

  test('a matching triple is verified, case-insensitively', () => {
    const finding = new Finding({ path: 'src/a.ts', line: 4, title: 'Off by one', severity: 'high' });
    stampVerification([finding], verification);
    expect(finding.verification).toBe('verified');
  });

  test('a claim the verifier did not mention is unverified, not fine', () => {
    const finding = new Finding({ path: 'src/b.ts', line: 4, title: 'Off by one', severity: 'high' });
    stampVerification([finding], verification);
    expect(finding.verification).toBe('unverified');
  });

  test('no verdict at all leaves nothing verified', () => {
    const finding = new Finding({ path: 'src/a.ts', line: 4, title: 'Off by one', severity: 'high' });
    stampVerification([finding], null);
    expect(finding.verification).toBe('unverified');
  });

  test('partition separates the reproduced from the rest', () => {
    const verified = new Finding({ path: 'src/a.ts', line: 4, title: 'Off by one', severity: 'high' });
    const other = new Finding({ path: 'src/c.ts', line: 9, title: 'Other', severity: 'low' });
    stampVerification([verified, other], verification);
    expect(partition([verified, other])).toEqual([[verified], [other]]);
  });

  test('the index keys on the triple `select` de-duplicates on', () => {
    const index = indexVerification(verification);
    expect(index.get('src/a.ts\n4\noff by one')?.status).toBe('verified');
  });
});
