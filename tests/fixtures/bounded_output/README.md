# `_bounded_output` fixtures — real producer bytes, not hand-typed ones (#4192)

Every file here is the **verbatim output of a real failing command**, captured
by running it, and is read by `scripts/ops-subprocess-output-smoke.py` and
`tests/unit/test_bounded_output_rescues_prefixed_lines.py`.

They exist because the previous tests for this mechanism supplied their own
fixtures, and those fixtures put the keyword at column 0
(`"error: 499"`, `"Error: Failed changing dylib ID of ..."`). No real producer
does. So `_OUTPUT_EXCERPT_SALIENT_RE` was anchored with `^`, every test passed,
and the rescue was vacuous against every tool ops actually shells out to — the
same shape of defect as a test whose fixture SHA decides its outcome. A fixture
that was *produced* rather than *written* cannot flatter the pattern that reads
it.

## `docker-build-npm-ci-network-failure.txt`

The `terraform-local-smoke` failure of 2026-10-21 on PR #4193
([run 39671548029](https://github.com/dkblinux98/nyxGPT/actions/runs/39671548029)):
`nyxgpt ops install --terraform --dev` → `docker build nyxgpt-web` → `npm ci`
fails to reach the registry. ops logged a bare `... [67 lines omitted] ...`
with npm's `npm error code <errno>` inside the omitted region, so the run said
nothing about *why* it failed.

Reproduced on an `ubuntu-latest` runner with the registry made unreachable
rather than waited for, so the capture is deterministic:

```sh
mkdir -p /tmp/npmfail && cd /tmp/npmfail
cat > package.json <<'EOF'
{"name":"probe","version":"1.0.0","dependencies":{"left-pad":"1.3.0"}}
EOF
# plus the matching lockfileVersion-3 package-lock.json for left-pad@1.3.0
{
  echo "FROM node:20-alpine"
  echo "WORKDIR /app"
  for i in $(seq 1 14); do echo "RUN mkdir -p /app/stage$i && echo layer$i > /app/stage$i/marker"; done
  echo "COPY package.json package-lock.json ./"
  echo "RUN npm ci"
} > Dockerfile
docker build --network none --no-cache --progress plain -t npmfail:probe . > capture.txt 2>&1
```

The fourteen `RUN mkdir` layers are padding with a purpose: they take the
output to **94 lines**, past `_OUTPUT_EXCERPT_VERBATIM_MAX_LINES`, so the
elision really happens — the real CI failure was 92 lines. Every byte is
BuildKit's and npm's own.

What makes it the right fixture is the prefixing, which is not special to this
command: each line carries BuildKit's `#23 90.52 ` *and* npm's `npm `, so the
keyword is never the first word.
