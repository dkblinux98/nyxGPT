import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'fs';
import { join } from 'path';

const CATALOG_SOURCE = readFileSync(
  join(__dirname, '../../src/components/DashboardCatalog.tsx'),
  'utf-8'
);

const DASHBOARDS_DIR = join(__dirname, '../../../docker/grafana/dashboards');

function catalogUids(): string[] {
  return [...CATALOG_SOURCE.matchAll(/uid: '([^']+)'/g)].map((match) => match[1]);
}

function provisionedUids(): string[] {
  return readdirSync(DASHBOARDS_DIR)
    .filter((file) => file.endsWith('.json'))
    .map((file) => JSON.parse(readFileSync(join(DASHBOARDS_DIR, file), 'utf-8')).uid);
}

describe('DashboardCatalog uid/dashboard consistency', () => {
  it('lists every dashboard provisioned under docker/grafana/dashboards', () => {
    const catalog = catalogUids();
    const provisioned = provisionedUids();

    for (const uid of provisioned) {
      expect(catalog).toContain(uid);
    }
  });

  it('does not reference a uid that is not actually provisioned', () => {
    const catalog = catalogUids();
    const provisioned = provisionedUids();

    for (const uid of catalog) {
      expect(provisioned).toContain(uid);
    }
  });
});
