// Build a Grafana Explore deep link that opens with the given LogQL query
// already loaded against the provisioned Loki datasource (uid "loki", pinned
// in docker/grafana/provisioning/datasources). Grafana's Explore state is
// URL-encodable (the `panes` param, stable since Grafana 10), which is what
// lets these curated queries be one click instead of copy/paste — Grafana has
// no file-provisioning for Explore saved queries. Shared by every page that
// links a Loki query into Explore (Log Aggregation panel, Self-Heal, Canary,
// Deploy) so there is exactly one implementation to keep correct.
export function exploreQueryUrl(exploreBase: string, query: string): string {
  const panes = {
    nyx: {
      datasource: 'loki',
      queries: [
        { refId: 'A', expr: query, queryType: 'range', datasource: { type: 'loki', uid: 'loki' } },
      ],
      range: { from: 'now-1h', to: 'now' },
    },
  };
  const url = new URL(exploreBase);
  url.searchParams.set('schemaVersion', '1');
  if (!url.searchParams.has('orgId')) {
    url.searchParams.set('orgId', '1');
  }
  url.searchParams.set('panes', JSON.stringify(panes));
  return url.toString();
}
