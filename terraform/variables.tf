variable "repo_path" {
  description = "Absolute path to the nyxGPT repository checkout, used as the Docker build context for the api/web images."
  type        = string
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

variable "api_image_tag" {
  description = "Tag applied to the built nyxgpt-api image."
  type        = string
  default     = "local"
}

variable "web_image_tag" {
  description = "Tag applied to the built nyxgpt-web image."
  type        = string
  default     = "local"
}

variable "web_api_base_url" {
  description = "API base URL baked into the web UI's client bundle at build time (Next.js NEXT_PUBLIC_* semantics)."
  type        = string
  default     = "http://localhost:8000"
}
