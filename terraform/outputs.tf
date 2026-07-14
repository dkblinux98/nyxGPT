output "api_url" {
  description = "Base URL for the nyxGPT FastAPI backend."
  value       = "http://localhost:${var.api_port}"
}

output "web_url" {
  description = "Base URL for the nyxGPT web UI."
  value       = "http://localhost:${var.web_port}"
}

output "ollama_url" {
  description = "Base URL for Ollama's API."
  value       = "http://localhost:${var.ollama_port}"
}
