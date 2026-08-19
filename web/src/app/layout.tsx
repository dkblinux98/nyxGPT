import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "../contexts/ThemeContext";
import { ToastProvider } from "../contexts/ToastContext";
import ClientErrorReporter from "../components/ClientErrorReporter";
import AppUpdateBanner from "../components/AppUpdateBanner";
import ServiceWorkerVersionGuard from "../components/ServiceWorkerVersionGuard";
import HydrationWatchdog from "../components/HydrationWatchdog";
import HydrationMarker from "../components/HydrationMarker";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "nyxGPT",
  description: "Your private AI assistant powered by local LLMs",
  icons: {
    icon: [
      { url: "/favicon-16x16.png", sizes: "16x16", type: "image/png" },
      { url: "/favicon-32x32.png", sizes: "32x32", type: "image/png" },
      { url: "/favicon.ico", sizes: "any" },
    ],
    shortcut: "/favicon.ico",
    apple: "/apple-touch-icon.png",
  },
  manifest: "/manifest.json",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  themeColor: "#0070f3",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <head>
        {/* Preload critical navigation routes so page transitions are instant.
            These are the routes users navigate to most often from the main
            chat page settings menu. */}
        <link rel="prefetch" href="/admin" as="document" />
        <link rel="prefetch" href="/models" as="document" />
        <link rel="prefetch" href="/settings" as="document" />
      </head>
      <body className={`${geistSans.variable} ${geistMono.variable}`}>
        {/* First in the body and outside every provider on purpose (#3857):
            this inline script must run even when none of the client bundle
            does, which is the state that leaves server-rendered loading
            placeholders on screen forever. HydrationMarker disarms it. */}
        <HydrationWatchdog />
        <ThemeProvider>
          <ToastProvider>
            <HydrationMarker />
            <ClientErrorReporter />
            <ServiceWorkerVersionGuard />
            <AppUpdateBanner />
            {children}
          </ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
