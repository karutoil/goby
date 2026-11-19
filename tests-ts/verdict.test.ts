import { describe, expect, test } from 'bun:test';
import { evaluateGate, parseReceipt, receiptMarker, type ReviewReceipt } from '../src/core/verdict.ts';

const head = 'a'.repeat(40);

function receipt(overrides: Partial<ReviewReceipt> = {}): ReviewReceipt {
  return {
    schema: 3, head_sha: head, base_ref: 'main', diff_sha256: 'b'.repeat(64),
    complete: true, agent: true, unthreaded: 0, finding_ids: ['abc123'],
    verify: 'required', verified_ids: ['abc123'], withheld: 0, withheld_high: 0, unverified: 0,
    ...overrides,
  };
}

describe('the receipt carries the verification verdict', () => {
  test('a schema-3 receipt round-trips', () => {
    expect(parseReceipt(receiptMarker(receipt()), head)).toEqual(receipt());
  });

  test('a schema-2 receipt is not misread as a schema-3 one', () => {
    const old = {
      schema: 2, head_sha: head, base_ref: 'main', diff_sha256: 'b'.repeat(64),
      complete: true, agent: true, unthreaded: 0, finding_ids: ['abc123'],
    };
    const body = `<!-- goby-gate ${JSON.stringify(old)} -->`;
    expect(parseReceipt(body, head)).toBeNull();
  });

  test('a missing or malformed verification field invalidates the receipt', () => {
    const body = (value: Record<string, unknown>) => `<!-- goby-gate ${JSON.stringify(value)} -->`;
    expect(parseReceipt(body({ ...receipt(), verify: 'sometimes' }), head)).toBeNull();
    expect(parseReceipt(body({ ...receipt(), withheld_high: -1 }), head)).toBeNull();
    expect(parseReceipt(body({ ...receipt(), verified_ids: ['abc123', 'abc123'] }), head)).toBeNull();
    const { verify: _verify, ...withoutVerify } = receipt() as unknown as Record<string, unknown>;
    expect(parseReceipt(body(withoutVerify), head)).toBeNull();
  });
});

describe('the gate reads verification as evidence, not as an input', () => {
  const evidence = { base_ref: 'main', diff_sha256: 'b'.repeat(64) };
  // Every posted finding becomes a thread, so the gate's `findings_accounted` fact sees them all;
  // `opened` adds unresolved threads on top of the ones this receipt published.
  const gate = (value: ReviewReceipt | null, policy: 'off' | 'advisory' | 'required', opts: { opened?: number; execution?: boolean } = {}) =>
    evaluateGate(value, head,
      Array.from({ length: opts.opened ?? 0 }, (_, i) => ({ fid: `extra${i}`, sha: head.slice(0, 10) })),
      (value?.finding_ids ?? []).map(fid => ({ fid, sha: head.slice(0, 10) })),
      true, opts.execution ?? true, evidence, { verify: policy });

  test('a required review that reproduced everything passes', () => {
    expect(gate(receipt(), 'required').state).toBe('success');
  });

  test('a high-severity claim nobody reproduced is a human decision, never a green merge', () => {
    const result = gate(receipt({ withheld: 1, withheld_high: 1 }), 'required');
    expect(result.state).toBe('failure');
    expect(result.description).toContain('could not be reproduced');
  });

  test('a low-severity claim withheld does not block', () => {
    expect(gate(receipt({ withheld: 2, withheld_high: 0 }), 'required').state).toBe('success');
  });

  test('a review run without verification cannot satisfy a gate that requires it', () => {
    const result = gate(receipt({ verify: 'off' }), 'required');
    expect(result.state).toBe('failure');
    expect(result.description).toContain("requires 'required'");
  });

  test('advisory does not enforce verification, and still ignores its counts', () => {
    expect(gate(receipt({ verify: 'advisory', withheld_high: 3 }), 'advisory').state).toBe('success');
  });

  test('a posted finding missing from the reproduction list invalidates the review', () => {
    const result = gate(receipt({ finding_ids: ['abc123'], verified_ids: [] }), 'required');
    expect(result.state).toBe('failure');
    expect(result.description).toContain('no reproduction');
  });

  test('verification is one fact among the existing ten', () => {
    expect(gate(receipt(), 'required', { opened: 1 }).state).toBe('failure');
    expect(gate(receipt(), 'required', { execution: false }).state).toBe('failure');
    expect(gate(null, 'required').state).toBe('failure');
  });
});
