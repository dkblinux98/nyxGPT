import { apiFetch } from "@/lib/apiProxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;
  const sessionName = decodeURIComponent(name);

  const res = await apiFetch(`/api/v1/sessions/${encodeURIComponent(sessionName)}/unpin`, {
    method: "POST",
  });

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
