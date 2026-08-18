variable "repo_path" {
  description = "Absolute path to a nyxGPT repository checkout, used as the Docker build context for the api/web images in dev mode (build_from_source = true). Empty -- the default -- is the artifact path, which has no checkout and builds nothing."
  type        = string
  default     = ""
}

# Dev mode (`nyxgpt ops install --terraform --local --dev`, #3835): build the
# api/web images from `repo_path`'s working tree instead of running the
# published `api_image`/`web_image`. False -- the default -- is the artifact
# path, the only one a machine with no checkout can run (CLAUDE.md's
# repo-less portability requirement).
variable "build_from_source" {
  description = "Build the api/web images from repo_path's working tree (dev mode) instead of using the published api_image/web_image."
  type        = bool
  default     = false
}

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

# Full image refs rather than a tag appended to a fixed name (#3835): the
# artifact path runs the published `ghcr.io/dkblinux98/nyxgpt-*` images, whose
# repository is not `nyxgpt-api`. `nyxgpt ops install --terraform --local`
# passes both on the command line -- the defaults are the dev-mode tags it
# builds locally, so a hand-run `terraform apply --var build_from_source=true`
# still behaves as it did before.
variable "api_image" {
  description = "Image ref for the nyxgpt api container -- a published image (artifact path) or the tag applied to the locally built one (dev mode)."
  type        = string
  default     = "nyxgpt-api:local"
}

variable "web_image" {
  description = "Image ref for the nyxgpt web container -- a published image (artifact path) or the tag applied to the locally built one (dev mode)."
  type        = string
  default     = "nyxgpt-web:local"
}

variable "web_api_base_url" {
  description = "API base URL baked into the web UI's client bundle at build time (Next.js NEXT_PUBLIC_* semantics)."
  type        = string
  default     = "http://localhost:8000"
}
