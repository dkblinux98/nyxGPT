import { describe, it, expect } from 'vitest';
import { apiErrorText, errorMessage } from '../../src/lib/apiError';

// #3831: every one of these shapes reached an error card as "[object Object]"
// before this helper existed -- the canary page's rollout failure hid
// "FailedScheduling: Insufficient memory" behind it.
describe('apiErrorText', () => {
  it.each([
    [
      "the API's own envelope",
      { error: { code: 'http_error', message: 'A rollout is already in progress', details: null } },
      'A rollout is already in progress',
    ],
    [
      'an envelope whose details name the real cause',
      {
        error: {
          code: 'http_error',
          message: 'Request failed',
          details: { errors: ['api.port: must be an integer', 'api.host: required'] },
        },
      },
      'Request failed: api.port: must be an integer; api.host: required',
    ],
    [
      'an envelope with a string detail payload',
      { error: { message: 'Rollout did not become healthy', details: 'Insufficient memory' } },
      'Rollout did not become healthy: Insufficient memory',
    ],
    [
      'an envelope carrying only details',
      { error: { details: 'Unschedulable: 0/1 nodes are available' } },
      'Unschedulable: 0/1 nodes are available',
    ],
    ['a bare string error', { error: 'kubectl not found' }, 'kubectl not found'],
    ["FastAPI's own refusal", { detail: 'Missing "enabled" field' }, 'Missing "enabled" field'],
    [
      "FastAPI's request-validation list",
      {
        detail: [
          { loc: ['body', 'weight_percent'], msg: 'value is not a valid integer', type: 'int' },
          { loc: ['body', 'component'], msg: 'field required' },
        ],
      },
      'body.weight_percent: value is not a valid integer; body.component: field required',
    ],
    ['a dict-shaped detail', { detail: { message: 'Cluster unreachable' } }, 'Cluster unreachable'],
    ['a bare message', { message: 'Nothing to promote' }, 'Nothing to promote'],
    ['whitespace around the message', { detail: '  padded  ' }, 'padded'],
  ])('reads %s', (_label, payload, expected) => {
    expect(apiErrorText(payload, 'HTTP 409')).toBe(expected);
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a non-object body', 'plain text'],
    ['a number body', 42],
    ['an unrecognised object', { unexpected: true }],
    ['an empty envelope', { error: {} }],
    ['an empty detail object', { detail: {} }],
    ['an envelope with a non-string message', { error: { code: 7 } }],
    ['an empty detail list', { detail: [] }],
    ['a blank message', { message: '   ' }],
  ])('falls back for %s', (_label, payload) => {
    expect(apiErrorText(payload, 'HTTP 500')).toBe('HTTP 500');
  });

  it('never renders an object as [object Object]', () => {
    const text = apiErrorText({ detail: { unexpected: 'shape', code: 12 } }, 'HTTP 500');
    expect(text).not.toContain('[object Object]');
    expect(text).toContain('unexpected');
  });

  it('renders unknown list entries as JSON rather than [object Object]', () => {
    const text = apiErrorText({ detail: [{ code: 12 }, null, 7] }, 'HTTP 500');
    expect(text).toBe('{"code":12}; 7');
  });

  it('survives a cyclic payload', () => {
    const cyclic: Record<string, unknown> = { code: 1 };
    cyclic.self = cyclic;
    expect(apiErrorText({ detail: cyclic }, 'HTTP 500')).toBe('HTTP 500');
  });

  it('renders a boolean or numeric details field', () => {
    expect(apiErrorText({ error: { message: 'nope', details: 42 } }, 'HTTP 500')).toBe('nope: 42');
    expect(apiErrorText({ error: { message: 'nope', details: false } }, 'HTTP 500')).toBe(
      'nope: false'
    );
  });

  it('renders a validation entry that has no loc', () => {
    expect(apiErrorText({ detail: [{ msg: 'field required' }] }, 'HTTP 422')).toBe(
      'field required'
    );
  });

  it('drops empty entries from a details list', () => {
    expect(apiErrorText({ detail: ['', 'real reason', null] }, 'HTTP 500')).toBe('real reason');
  });

  it('reads a nested error/msg key inside details', () => {
    expect(apiErrorText({ detail: { msg: 'inner msg' } }, 'HTTP 500')).toBe('inner msg');
    expect(apiErrorText({ error: { details: { error: 'inner error' } } }, 'HTTP 500')).toBe(
      'inner error'
    );
  });
});

describe('errorMessage', () => {
  it('reads an Error', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom');
  });

  it('falls back for an Error with no message', () => {
    expect(errorMessage(new Error(''), 'HTTP 500')).toBe('HTTP 500');
  });

  it('reads a thrown string', () => {
    expect(errorMessage('network exploded')).toBe('network exploded');
  });

  it('falls back for a blank thrown string', () => {
    expect(errorMessage('   ')).toBe('Request failed');
  });

  it('unwraps a thrown payload instead of rendering [object Object]', () => {
    expect(errorMessage({ error: { message: 'Insufficient memory' } })).toBe(
      'Insufficient memory'
    );
    expect(errorMessage({ nothing: 'useful' })).toBe('Request failed');
  });

  it('falls back for null and undefined, and stringifies other primitives', () => {
    expect(errorMessage(null)).toBe('Request failed');
    expect(errorMessage(undefined)).toBe('Request failed');
    expect(errorMessage(42)).toBe('42');
  });
});
