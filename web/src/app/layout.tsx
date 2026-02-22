import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { ThemeProvider } from "../contexts/ThemeContext";
import { ToastProvider } from "../contexts/ToastContext";

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
    icon: "/stone-soup-creative-logo.png",
    shortcut: "/stone-soup-creative-logo.png",
    apple: "/stone-soup-creative-logo.png",
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
        <ThemeProvider>
          <ToastProvider>
            {children}
          </ToastProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
