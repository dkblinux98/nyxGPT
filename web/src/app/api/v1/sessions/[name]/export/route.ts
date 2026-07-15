import { NextRequest } from 'next/server';
import { apiFetch } from '@/lib/apiProxy';

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const format = request.nextUrl.searchParams.get('format') ?? 'markdown';

  const res = await apiFetch(
    `/api/v1/sessions/${encodeURIComponent(name)}/export?format=${encodeURIComponent(format)}`,
    { cache: 'no-store' }
  );

  const headers: Record<string, string> = {
    'Content-Type': res.headers.get('Content-Type') ?? 'application/octet-stream',
  };
  const contentDisposition = res.headers.get('Content-Disposition');
  if (contentDisposition) {
    headers['Content-Disposition'] = contentDisposition;
  }

  return new Response(res.body, { status: res.status, headers });
}
