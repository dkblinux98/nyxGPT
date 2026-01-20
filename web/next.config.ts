import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Next.js dev server may warn about cross-origin requests to /_next/*.
  // Allow local origins used during development.
  // Next expects origins (scheme+host) in newer versions, but we keep host-only entries too.
  allowedDevOrigins: [
    "localhost",
    "127.0.0.1",
    "192.168.86.21",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://192.168.86.21:3000",
  ],

  // Position dev indicator in top-right to avoid covering app UI
  devIndicators: {
    buildActivity: true,
    buildActivityPosition: 'top-right',
  },
};

export default nextConfig;
