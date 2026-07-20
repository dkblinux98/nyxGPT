# Proposed Architecture

Forward-looking architecture discussion for nyxGPT. Current-state architecture
and its invariants are documented in [`docs/architecture.md`](../docs/architecture.md);
anything here is proposal, not commitment.

## Candidate extensions

The architecture intentionally supports additional features:

- Pluggable memory backends
- Additional vector databases beyond Cassandra
- Multi-user authentication and authorization
- Advanced context window management strategies
- Custom tool/function calling

No major architectural changes are required to add these features.
