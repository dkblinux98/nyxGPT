import { apiFetch } from "@/lib/apiProxy";

export async function POST() {
  try {
    const res = await apiFetch(`/api/v1/infra/terraform/down`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
    });

    const data = await res.json();

    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    console.error("Failed to destroy the Terraform stack:", error);
    return new Response(
      JSON.stringify({ error: "Failed to destroy the Terraform stack" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
