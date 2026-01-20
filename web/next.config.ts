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

  // Disable dev indicators to avoid covering app UI
  // The indicator position in Next.js 16 is not reliably configurable
  devIndicators: {
    buildActivity: false,
    buildActivityPosition: 'top-right' as any,
  },
};

export default nextConfig;
