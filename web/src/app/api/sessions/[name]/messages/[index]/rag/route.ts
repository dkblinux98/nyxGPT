import { apiFetch } from "@/lib/apiProxy";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ name: string; index: string }> }
) {
  const { name, index } = await params;
  const sessionName = decodeURIComponent(name);
  const messageIndex = decodeURIComponent(index);

  const res = await apiFetch(
    `/api/v1/sessions/${encodeURIComponent(sessionName)}/messages/${messageIndex}/rag`,
    {
      cache: "no-store",
    }
  );

  return new Response(res.body, {
    status: res.status,
    headers: {
      "Content-Type": "application/json",
    },
  });
}
