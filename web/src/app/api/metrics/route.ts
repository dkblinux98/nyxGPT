import { NextResponse } from 'next/server';
import { apiFetch } from '@/lib/apiProxy';

export async function GET() {
  try {
    const res = await apiFetch('/api/v1/metrics', {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
    });

    if (!res.ok) {
      return NextResponse.json(
        { error: `Backend API returned ${res.status}` },
        { status: res.status }
      );
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch (error) {
    console.error('Error fetching metrics:', error);
    return NextResponse.json(
      { error: 'Failed to fetch resource metrics' },
      { status: 500 }
    );
  }
}
