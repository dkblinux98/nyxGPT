import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Next.js dev server may warn about cross-origin requests to /_next/*.
  // Explicitly allow local origins used during development.
  allowedDevOrigins: ["http://localhost:3000", "http://127.0.0.1:3000"],
};

export default nextConfig;
