import { apiFetch } from "@/lib/apiProxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const sessionName = decodeURIComponent(name);

  // Get request body
  const body = await request.json();

  const res = await apiFetch(`/api/v1/sessions/${encodeURIComponent(sessionName)}/rename`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
