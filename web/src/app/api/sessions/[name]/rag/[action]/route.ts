import { apiFetch } from "@/lib/apiProxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ name: string; action: string }> }
) {
  const { name, action } = await params;
  const sessionName = decodeURIComponent(name);

  if (action !== 'enable' && action !== 'disable') {
    return new Response(JSON.stringify({ error: 'Invalid action' }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const res = await apiFetch(
    `/api/v1/sessions/${encodeURIComponent(sessionName)}/rag/${action}`,
    {
      method: "POST",
    }
  );

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
