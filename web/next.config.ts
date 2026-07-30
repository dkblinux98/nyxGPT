import type { NextConfig } from "next";
import withPWA from "@ducanh2912/next-pwa";
import withBundleAnalyzer from "@next/bundle-analyzer";

export const imageConfig = {
  formats: ["image/avif", "image/webp"] as Array<"image/avif" | "image/webp">,
  deviceSizes: [640, 750, 828, 1080, 1200, 1920],
  imageSizes: [16, 32, 48, 64, 96, 128, 256],
};

const nextConfig: NextConfig = {
  images: imageConfig,
  // Lets Next.js tree-shake named imports from these packages down to only
  // the members actually used, instead of pulling in the whole module.
  experimental: {
    optimizePackageImports: ["react-virtuoso"],
  },
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

const analyzedConfig = withBundleAnalyzer({
  enabled: process.env.ANALYZE === "true",
})(nextConfig);

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
      // Background sync: queue session mutations (rename, pin/unpin, delete,
      // title, filename sync) made while offline and replay them once
      // connectivity returns, instead of just failing the request. These
      // routes are safe to queue because the UI already applies optimistic
      // updates and the requests are small, idempotent-ish state changes.
      //
      // Each `backgroundSync` entry below gets its own Workbox `Queue`
      // instance (one per runtimeCaching route, keyed on `method` since a
      // Route only matches one HTTP method), and Workbox throws
      // `duplicate-queue-name` if two `Queue`s in the same generated
      // service-worker share a `name` -- it happened here (#3445) because
      // both the POST and DELETE routes used "session-mutations-queue". The
      // queue names must stay distinct.
      {
        urlPattern: /^\/api\/sessions\/[^/]+\/(rename|title|pin|unpin|delete|sync-filename)$/i,
        method: "POST",
        handler: "NetworkOnly",
        options: {
          backgroundSync: {
            name: "session-mutations-queue",
            options: {
              maxRetentionTime: 24 * 60, // 24 hours in minutes
            },
          },
        },
      },
      {
        urlPattern: /^\/api\/sessions\/[^/]+\/documents\/[^/]+$/i,
        method: "DELETE",
        handler: "NetworkOnly",
        options: {
          backgroundSync: {
            name: "document-mutations-queue",
            options: {
              maxRetentionTime: 24 * 60, // 24 hours in minutes
            },
          },
        },
      },
    ],
  },
})(analyzedConfig);
