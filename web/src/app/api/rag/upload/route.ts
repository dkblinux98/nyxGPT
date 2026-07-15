import { apiFetch } from "@/lib/apiProxy";

export async function POST(request: Request) {
  const formData = await request.formData();

  const res = await apiFetch(`/api/v1/rag/upload`, {
    method: "POST",
    body: formData,
  });

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
