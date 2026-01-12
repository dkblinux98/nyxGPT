import { NextRequest } from 'next/server';

const BASE_URL = process.env.MYGPT_API_BASE_URL ?? 'http://127.0.0.1:8000';

export async function GET() {
  try {
    const r = await fetch(`${BASE_URL}/api/v1/config`, {
      cache: 'no-store',
    });

    return new Response(r.body, {
      status: r.status,
      headers: {
        'Content-Type': 'application/json',
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
      suggestion = `Check the MYGPT_API_BASE_URL environment variable (currently: ${BASE_URL})`;
    }

    return new Response(
      JSON.stringify({
        error: {
          message: userFriendlyMessage,
          suggestion: suggestion,
          details: errorMessage,
          api_url: BASE_URL,
        },
      }),
      {
        status: 503,
        headers: {
          'Content-Type': 'application/json',
        },
      }
    );
  }
}

export async function POST(request: Request) {
  const body = await request.json();

  try {
    const r = await fetch(`${BASE_URL}/api/v1/config`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
      cache: 'no-store',
    });

    return new Response(r.body, {
      status: r.status,
      headers: {
        'Content-Type': 'application/json',
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
      suggestion = `Check the MYGPT_API_BASE_URL environment variable (currently: ${BASE_URL})`;
    }

    return new Response(
      JSON.stringify({
        error: {
          message: userFriendlyMessage,
          suggestion: suggestion,
          details: errorMessage,
          api_url: BASE_URL,
        },
      }),
      {
        status: 503,
        headers: {
          'Content-Type': 'application/json',
        },
      }
    );
  }
}
