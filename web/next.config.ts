import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  allowedDevOrigins: [
    "localhost",
    "127.0.0.1",
    "192.168.86.21",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://192.168.86.21:3000",
  ],
  devIndicators: {
    position: "top-right", // top-right, bottom-right, top-left, bottom-left
  },
  images: {
    formats: ['image/webp', 'image/avif'],
    deviceSizes: [640, 750, 828, 1080, 1200, 1920, 2048, 3840],
    imageSizes: [16, 32, 48, 64, 96, 128, 256, 384],
    minimumCacheTTL: 60,
  },
};
export default nextConfig;
