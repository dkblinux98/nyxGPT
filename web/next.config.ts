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

  // Keep dev indicators visible but position in bottom-right
  // Provides access to preferences and shows compiling status
  devIndicators: {
    buildActivity: true,
    buildActivityPosition: 'bottom-right' as any,
  },
};

export default nextConfig;
