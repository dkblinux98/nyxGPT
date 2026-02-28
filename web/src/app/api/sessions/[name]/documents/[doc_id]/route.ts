const BASE = process.env.NYXGPT_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ name: string; doc_id: string }> }
) {
  const { name, doc_id } = await params;
  const sessionName = decodeURIComponent(name);
  const docId = decodeURIComponent(doc_id);

  const res = await fetch(
    `${BASE}/api/v1/sessions/${encodeURIComponent(sessionName)}/documents/${encodeURIComponent(docId)}`,
    { method: "DELETE" }
  );

  return new Response(res.body, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}
