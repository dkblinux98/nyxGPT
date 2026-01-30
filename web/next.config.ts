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

  // Code splitting and chunk optimization
  // Note: Next.js 16+ uses Turbopack by default which already optimizes chunks
  // We enable an empty turbopack config to satisfy the build requirement
  turbopack: {},

  experimental: {
    // Enable optimistic client cache to reduce bundle size
    optimisticClientCache: true,
  },

  // Webpack configuration for chunk optimization (used when --webpack flag is passed)
  webpack: (config, { isServer }) => {
    if (!isServer) {
      // Optimize chunk splitting
      config.optimization = {
        ...config.optimization,
        splitChunks: {
          chunks: 'all',
          cacheGroups: {
            // Vendor chunk for node_modules
            vendor: {
              test: /[\\/]node_modules[\\/]/,
              name: 'vendors',
              priority: 10,
              reuseExistingChunk: true,
            },
            // Common chunk for shared code
            common: {
              minChunks: 2,
              priority: 5,
              reuseExistingChunk: true,
              name: 'common',
            },
            // Separate chunk for React and React-DOM
            react: {
              test: /[\\/]node_modules[\\/](react|react-dom)[\\/]/,
              name: 'react',
              priority: 20,
              reuseExistingChunk: true,
            },
          },
        },
      };
    }

    return config;
  },
};

export default nextConfig;
