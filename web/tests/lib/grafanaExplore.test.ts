import { describe, it, expect } from 'vitest';
import { exploreQueryUrl } from '../../src/lib/grafanaExplore';

describe('exploreQueryUrl', () => {
  it('merges schemaVersion/orgId/panes into a base with no existing query string', () => {
    const plain = new URL(exploreQueryUrl('http://localhost:3001/explore', '{job="nyxgpt"}'));
    expect(plain.searchParams.get('schemaVersion')).toBe('1');
    expect(plain.searchParams.get('orgId')).toBe('1');
    expect(plain.searchParams.get('panes')).toBeTruthy();
  });

  it('merges into an existing query string without duplicating keys', () => {
    const withQuery = new URL(
      exploreQueryUrl('http://localhost:3001/explore?orgId=1', '{job="nyxgpt"}')
    );
    expect(withQuery.searchParams.getAll('orgId')).toEqual(['1']);
    expect(withQuery.searchParams.get('schemaVersion')).toBe('1');
  });

  it('preserves a non-default orgId already present on the configured base', () => {
    const withOrg = new URL(
      exploreQueryUrl('http://localhost:3001/explore?orgId=7&kiosk=tv', '{job="nyxgpt"}')
    );
    expect(withOrg.searchParams.getAll('orgId')).toEqual(['7']);
    expect(withOrg.searchParams.get('kiosk')).toBe('tv');
  });

  it('round-trips the query and time range through the encoded panes state', () => {
    const url = exploreQueryUrl('http://localhost:3001/explore', '{job="nyxgpt"} |= `x`');
    const panes = JSON.parse(new URL(url).searchParams.get('panes')!);
    expect(panes.nyx.datasource).toBe('loki');
    expect(panes.nyx.queries[0].expr).toBe('{job="nyxgpt"} |= `x`');
    expect(panes.nyx.range).toEqual({ from: 'now-1h', to: 'now' });
  });
});
