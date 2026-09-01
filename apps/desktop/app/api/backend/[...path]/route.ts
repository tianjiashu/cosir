import { NextRequest, NextResponse } from "next/server";

const backendBaseUrl = (
  process.env.COSIR_BACKEND_URL ??
  "http://127.0.0.1:8000"
).replace(/\/$/, "");

const hopByHopHeaders = new Set([
  "connection",
  "content-length",
  "host",
  "keep-alive",
  "transfer-encoding",
  "upgrade",
]);

const STARTUP_RETRY_COUNT = 20;
const STARTUP_RETRY_DELAY_MS = 250;

function canRetryDuringStartup(method: string): boolean {
  return method === "GET" || method === "HEAD";
}

async function fetchBackend(
  target: string,
  request: NextRequest,
  options: RequestInit,
): Promise<Response> {
  const attempts = canRetryDuringStartup(request.method)
    ? STARTUP_RETRY_COUNT
    : 1;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await fetch(target, options);
    } catch (error) {
      if (attempt === attempts - 1) throw error;
      await new Promise((resolve) =>
        setTimeout(resolve, STARTUP_RETRY_DELAY_MS),
      );
    }
  }
  throw new Error("Backend request retry loop ended unexpectedly");
}

async function proxy(request: NextRequest, path: string[]) {
  const target = `${backendBaseUrl}/${path.join("/")}${request.nextUrl.search}`;
  const headers = new Headers(request.headers);
  for (const header of hopByHopHeaders) headers.delete(header);

  const body = ["GET", "HEAD"].includes(request.method)
    ? undefined
    : await request.arrayBuffer();
  let response: Response;
  try {
    response = await fetchBackend(target, request, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
    });
  } catch {
    return NextResponse.json(
      { error: { code: "backend_unavailable", message: "本地 Agent 后端尚未就绪，请稍后重试" } },
      { status: 503 },
    );
  }

  const responseHeaders = new Headers();
  response.headers.forEach((value, key) => {
    if (!hopByHopHeaders.has(key)) responseHeaders.set(key, value);
  });

  return new NextResponse(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
}

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return proxy(request, (await context.params).path);
}

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return proxy(request, (await context.params).path);
}

export async function PUT(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return proxy(request, (await context.params).path);
}

export async function PATCH(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return proxy(request, (await context.params).path);
}

export async function DELETE(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  return proxy(request, (await context.params).path);
}
