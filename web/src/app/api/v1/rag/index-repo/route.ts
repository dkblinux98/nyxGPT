import { apiFetch } from "@/lib/apiProxy";

export async function POST(request: Request) {
  try {
    const body = await request.json();

    // Validate required fields
    if (!body.repo_path || typeof body.repo_path !== "string") {
      return new Response(
        JSON.stringify({ detail: "repo_path is required and must be a string" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    // Validate repo_path is not empty
    if (body.repo_path.trim().length === 0) {
      return new Response(
        JSON.stringify({ detail: "repo_path cannot be empty" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    // Validate extensions if provided
    if (body.extensions !== undefined && body.extensions !== null) {
      if (!Array.isArray(body.extensions)) {
        return new Response(
          JSON.stringify({ detail: "extensions must be an array" }),
          {
            status: 400,
            headers: { "Content-Type": "application/json" },
          }
        );
      }
      // Validate each extension is a string
      for (const ext of body.extensions) {
        if (typeof ext !== "string") {
          return new Response(
            JSON.stringify({ detail: "all extensions must be strings" }),
            {
              status: 400,
              headers: { "Content-Type": "application/json" },
            }
          );
        }
      }
    }

    // Validate boolean fields
    if (body.extract_docs_only !== undefined && typeof body.extract_docs_only !== "boolean") {
      return new Response(
        JSON.stringify({ detail: "extract_docs_only must be a boolean" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    if (body.ensure_schema !== undefined && typeof body.ensure_schema !== "boolean") {
      return new Response(
        JSON.stringify({ detail: "ensure_schema must be a boolean" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        }
      );
    }

    const res = await apiFetch(`/api/v1/rag/index-repo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const errorData = await res.json().catch(() => ({ error: "Unknown error" }));
      return new Response(JSON.stringify(errorData), {
        status: res.status,
        headers: { "Content-Type": "application/json" },
      });
    }

    const data = await res.json();
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (error) {
    console.error("Failed to index repository:", error);
    return new Response(
      JSON.stringify({ detail: "Failed to index repository via backend" }),
      {
        status: 502,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
}
