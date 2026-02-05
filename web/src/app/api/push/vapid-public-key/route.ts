import { NextResponse } from "next/server";

export async function GET() {
  // TODO: Implement VAPID key generation and storage
  // For now, return a placeholder response indicating the feature is not yet configured
  return NextResponse.json(
    {
      error: "Push notifications not configured",
      message:
        "VAPID keys need to be generated and configured on the server. See docs/pwa-implementation.md for setup instructions.",
    },
    { status: 501 }
  );
}
