# `repo_path` and `build_from_source` were RETIRED by #3984, along with the
# `dynamic "build"` blocks they drove in main.tf. Nothing here builds an image
# any more, in either install mode: `nyxgpt ops install --terraform` builds
# (dev) or stages-and-builds (artifact) before it ever runs `terraform apply`,
# and passes the finished tag in `api_image`/`web_image`. Re-adding either
# variable is re-adding the provider build that cannot complete on Docker 29.x
# with the containerd image store -- see `docker_image.api` in main.tf.

variable "ollama_port" {
  description = "Host port for Ollama's API."
  type        = number
  default     = 11434
}

variable "cassandra_port" {
  description = "Host port for Cassandra's CQL native protocol."
  type        = number
  default     = 9042
}

variable "api_port" {
  description = "Host port for the nyxGPT FastAPI backend."
  type        = number
  default     = 8000
}

variable "web_port" {
  description = "Host port for the nyxGPT Next.js web UI."
  type        = number
  default     = 3000
}

variable "auth_api_key" {
  description = "Shared secret for the API's [auth] section (see docs/security.md). Required since the web container reaches the API over the Docker network rather than localhost."
  type        = string
  sensitive   = true
}

variable "cors_origins" {
  description = "Comma-separated origins allowed to call the API via CORS."
  type        = string
  default     = "http://localhost:3000,http://127.0.0.1:3000"
}

# Full image refs rather than a tag appended to a fixed name (#3835): an
# operator may point either container at any image their daemon can resolve,
# including a `ghcr.io/...` one, and that repository is not `nyxgpt-api`.
#
# `nyxgpt ops install --terraform` always passes both on the command line: the
# `:local` tags it builds in dev mode, or the version-qualified
# `nyxgpt-api:artifact-<version>` / `nyxgpt-web:artifact-<version>` tags it
# builds from the published source tarballs on the artifact path (#3985). The
# defaults below are the dev-mode tags, so a hand-run `terraform apply` after
# a `nyxgpt up --terraform --dev` needs no `-var` at all. Whatever is named
# here must already exist locally or be pullable -- this configuration builds
# nothing (#3984).
variable "api_image" {
  description = "Image ref for the nyxgpt api container. Must already be built or pullable -- `nyxgpt ops install --terraform` produces it before apply."
  type        = string
  default     = "nyxgpt-api:local"
}

variable "web_image" {
  description = "Image ref for the nyxgpt web container. Must already be built or pullable -- `nyxgpt ops install --terraform` produces it before apply."
  type        = string
  default     = "nyxgpt-web:local"
}

# `web_api_base_url` was RETIRED by #3984 with the web `build {}` block that
# was its only consumer. The API base URL baked into the web bundle is now a
# build arg ops passes when it builds the image
# (`TF_WEB_API_BASE_URL_DEFAULT`, src/nyxgpt/ops.py).
