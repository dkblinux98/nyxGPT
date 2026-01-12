export async function GET() {
  const base =
    process.env.MYGPT_API_BASE_URL ??
    "http://127.0.0.1:8000";

  try {
    const r = await fetch(`${base}/api/v1/info`, {
      cache: "no-store",
    });

    return new Response(r.body, {
      status: r.status,
      headers: {
        "Content-Type": "application/json",
      },
    });
  } catch (error: unknown) {
    const errorMessage = error instanceof Error ? error.message : String(error);

    // Determine the likely cause of the error
    let userFriendlyMessage = 'Failed to connect to the myGPT API.';
    let suggestion = 'Please ensure the API service is running.';

    if (errorMessage.includes('ECONNREFUSED') || errorMessage.includes('fetch failed')) {
      userFriendlyMessage = 'The myGPT API service is not running or not accessible.';
      suggestion = 'Start the API service with: mygpt ops restart api';
    } else if (errorMessage.includes('ETIMEDOUT') || errorMessage.includes('timeout')) {
      userFriendlyMessage = 'Connection to the myGPT API timed out.';
      suggestion = 'Check if the API service is responding slowly or if there are network issues.';
    } else if (errorMessage.includes('ENOTFOUND') || errorMessage.includes('getaddrinfo')) {
      userFriendlyMessage = 'Cannot resolve the API hostname.';
      suggestion = `Check the MYGPT_API_BASE_URL environment variable (currently: ${base})`;
    }

    return new Response(
      JSON.stringify({
        error: {
          message: userFriendlyMessage,
          suggestion: suggestion,
          details: errorMessage,
          api_url: base,
        },
      }),
      {
        status: 503,
        headers: {
          "Content-Type": "application/json",
        },
      }
    );
  }
}
