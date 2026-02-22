import type { NextConfig } from "next";
import withPWA from "@ducanh2912/next-pwa";

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
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  webpack: (config: any) => {
    // Chunk optimization: split react-virtuoso into a dedicated vendor chunk so
    // it is loaded only when the virtualized list is needed, not in the initial
    // JS payload for every page.
    const splitChunks = config?.optimization?.splitChunks as
      | Record<string, unknown>
      | false
      | undefined;
    if (splitChunks && typeof splitChunks === "object") {
      const existingGroups =
        (splitChunks.cacheGroups as Record<string, unknown>) ?? {};
      splitChunks.cacheGroups = {
        ...existingGroups,
        // Isolate react-virtuoso so it is only fetched when the
        // VirtualizedSessionList or ChatPane chunks are loaded.
        virtuosoVendor: {
          test: /[\\/]node_modules[\\/]react-virtuoso[\\/]/,
          name: "vendor-virtuoso",
          chunks: "all",
          priority: 30,
          reuseExistingChunk: true,
        },
      };
    }
    return config;
  },
};

export default withPWA({
  dest: "public",
  disable: process.env.NODE_ENV === "development",
  register: true,
  scope: "/",
  sw: "sw.js",
  fallbacks: {
    document: "/offline",
  },
  workboxOptions: {
    runtimeCaching: [
      {
        urlPattern: /^https:\/\/fonts\.(?:gstatic|googleapis)\.com\/.*/i,
        handler: "CacheFirst",
        options: {
          cacheName: "google-fonts",
          expiration: {
            maxEntries: 4,
            maxAgeSeconds: 365 * 24 * 60 * 60, // 1 year
          },
        },
      },
      {
        urlPattern: /^https:\/\/.+\.(png|jpg|jpeg|svg|gif|webp)$/i,
        handler: "StaleWhileRevalidate",
        options: {
          cacheName: "static-images",
          expiration: {
            maxEntries: 64,
            maxAgeSeconds: 30 * 24 * 60 * 60, // 30 days
          },
        },
      },
      {
        urlPattern: /\.(?:js|css)$/i,
        handler: "StaleWhileRevalidate",
        options: {
          cacheName: "static-resources",
        },
      },
      {
        urlPattern: /^\/api\/.*/i,
        handler: "NetworkFirst",
        options: {
          cacheName: "api-cache",
          networkTimeoutSeconds: 10,
          expiration: {
            maxEntries: 50,
            maxAgeSeconds: 5 * 60, // 5 minutes
          },
        },
      },
    ],
  },
})(nextConfig);
