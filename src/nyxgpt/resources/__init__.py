"""Packaged runtime data for the ops layer (#3621).

Docker Compose file, config/provisioning templates, launchd/systemd unit
templates, and helper scripts, resolved via `importlib.resources` so
`nyxgpt.ops` doesn't depend on a source checkout. The files under this
package are symlinks back to their canonical top-level locations
(`docker/`, `docker-compose.yml`, `ops/`, `.env.example`,
`scripts/*.sh`) so there is exactly one place to edit each one;
setuptools dereferences the symlinks when it builds the wheel/sdist, so
the built package contains real file content, not links. See
`nyxgpt.ops._sync_packaged_resources`.
"""
