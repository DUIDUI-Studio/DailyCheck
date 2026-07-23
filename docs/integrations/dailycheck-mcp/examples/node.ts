/**
 * DailyCheck MCP — minimal TypeScript reference client.
 *
 * Zero dependencies: uses only the global `fetch` (Node 18+, Bun, Deno, Workers).
 *
 * Run as:
 *   DAILYCHECK_MCP_URL=http://localhost:5100 \
 *   DAILYCHECK_MCP_TOKEN=dev-mcp-token-for-testing \
 *   npx ts-node node.ts
 *   # or: bun run node.ts
 *
 * Behavior:
 *   1. initialize           → confirm protocol/server
 *   2. tools/list           → load the tool registry
 *   3. warehouse_list       → fetch warehouse codes
 *   4. warehouse_consumption → demo call (wh_001, 7d, top 10 by turnover)
 *   5. trigger and handle every documented error envelope
 */

export interface McpResponse<T = unknown> {
  jsonrpc: "2.0";
  id: number;
  result?: T;
  error?: { code: number; message: string; data?: unknown };
}

export interface ToolCallResult<T = unknown> {
  content: Array<{ type: "text"; text: string }>;
  isError: boolean;
}

export class JsonRpcError extends Error {
  constructor(public readonly code: number, message: string) {
    super(`JSON-RPC ${code}: ${message}`);
  }
}

export class ToolError extends Error {
  constructor(public readonly code: string, message: string) {
    super(`tool ${code}: ${message}`);
  }
}

interface ClientOpts {
  baseUrl: string;
  token: string;
  fetchImpl?: typeof fetch;
}

export class DailyCheckMcp {
  private readonly endpoint: string;
  private readonly token: string;
  private readonly fetchImpl: typeof fetch;
  private nextId = 1;

  constructor(opts: ClientOpts) {
    this.endpoint = `${opts.baseUrl.replace(/\/$/, "")}/api/mcp/`;
    this.token = opts.token;
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  /** Low-level transport — returns parsed JSON response. Throws on HTTP 401. */
  private async _post(body: Record<string, unknown>): Promise<McpResponse> {
    const res = await this.fetchImpl(this.endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.token}`,
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });

    if (res.status === 401) {
      throw new Error("401 Unauthorized — check DAILYCHECK_MCP_TOKEN");
    }
    if (res.status === 202) {
      // notifications accepted, no body
      return { jsonrpc: "2.0", id: (body.id as number) ?? 0, result: {} };
    }
    if (!res.ok) {
      // 400 / 406 — body is JSON-RPC envelope error
      const text = await res.text();
      try {
        return JSON.parse(text);
      } catch {
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 500)}`);
      }
    }
    return (await res.json()) as McpResponse;
  }

  /** High-level MCP call. Returns parsed tool payload on success. */
  async call<T = unknown>(
    method: string,
    params?: Record<string, unknown>
  ): Promise<T> {
    const id = this.nextId++;
    const body: Record<string, unknown> = { jsonrpc: "2.0", id, method };
    if (params !== undefined) body.params = params;

    const resp = await this._post(body);

    // §1 — JSON-RPC envelope error
    if (resp.error) {
      throw new JsonRpcError(resp.error.code, resp.error.message);
    }

    const result = (resp.result ?? {}) as ToolCallResult;

    // §2 — explicit isError
    if (result.isError) {
      const text = result.content?.[0]?.text ?? "";
      try {
        const parsed = JSON.parse(text);
        if (parsed?.error && parsed?.message) {
          throw new ToolError(parsed.error, parsed.message);
        }
        throw new ToolError("unknown", text);
      } catch (e) {
        if (e instanceof ToolError) throw e;
        throw new ToolError("unknown_tool_or_unparseable", text);
      }
    }

    // Some tools forget isError — detect by inner JSON envelope
    const inner = result.content?.[0]?.text;
    if (typeof inner === "string") {
      try {
        const parsed = JSON.parse(inner);
        if (
          parsed &&
          typeof parsed === "object" &&
          typeof parsed.error === "string" &&
          typeof parsed.message === "string"
        ) {
          throw new ToolError(parsed.error, parsed.message);
        }
      } catch (e) {
        if (e instanceof ToolError) throw e;
      }
    }

    // tools/call → parse JSON out of text; other methods return raw
    if (method === "tools/call" && typeof inner === "string") {
      return JSON.parse(inner) as T;
    }
    return (result as unknown) as T;
  }
}

// -------- demo --------

const URL_ = process.env.DAILYCHECK_MCP_URL ?? "http://localhost:5100";
const TOKEN_ = process.env.DAILYCHECK_MCP_TOKEN ?? "";
if (!TOKEN_) {
  console.error("DAILYCHECK_MCP_TOKEN env var is required");
  process.exit(2);
}

const mcp = new DailyCheckMcp({ baseUrl: URL_, token: TOKEN_ });

async function main() {
  console.log("=".repeat(60));
  console.log("1. initialize");
  console.log("=".repeat(60));
  const info = await mcp.call<any>("initialize", {
    protocolVersion: "2025-03-26",
    capabilities: {},
    clientInfo: { name: "node-smoke", version: "0.1.0" },
  });
  console.log(`server: ${info.serverInfo.name} v${info.serverInfo.version}`);

  console.log();
  console.log("=".repeat(60));
  console.log("2. tools/list — registry snapshot");
  console.log("=".repeat(60));
  const registry = await mcp.call<any>("tools/list");
  console.log(
    `  ${registry.tools.length} tools: ${registry.tools
      .map((t: any) => t.name)
      .join(", ")}`
  );

  console.log();
  console.log("=".repeat(60));
  console.log("3. tools/call warehouse_list");
  console.log("=".repeat(60));
  const warehouses = await mcp.call<any[]>("tools/call", {
    name: "warehouse_list",
    arguments: {},
  });
  for (const w of warehouses) {
    console.log(`  - ${w.code.padEnd(10)} ${w.name}`);
  }

  console.log();
  console.log("=".repeat(60));
  console.log(
    "4. tools/call warehouse_consumption (wh_001, days=7, top 10 by turnover)"
  );
  console.log("=".repeat(60));
  const rows = await mcp.call<any[]>("tools/call", {
    name: "warehouse_consumption",
    arguments: {
      warehouse_code: "wh_001",
      days: 7,
      sort_by: "turnover",
      limit: 10,
    },
  });
  console.log(`  ${rows.length} items (showing up to 5):`);
  for (const r of rows.slice(0, 5)) {
    console.log(
      `  - rank=${String(r.rank).padStart(2)}  sku=${r.sku.padEnd(30)}  ` +
        `consume_qty=${String(r.consume_qty).padEnd(6)}  ` +
        `current_stock=${String(r.current_stock).padEnd(6)}  ` +
        `turnover_rate=${r.turnover_rate}`
    );
  }

  console.log();
  console.log("=".repeat(60));
  console.log("5. error envelopes — every documented failure");
  console.log("=".repeat(60));
  const cases: Array<[string, Record<string, unknown>]> = [
    ["missing warehouse_code", { name: "warehouse_consumption", arguments: {} }],
    [
      "bad days value",
      {
        name: "warehouse_consumption",
        arguments: { warehouse_code: "wh_001", days: 99 },
      },
    ],
    ["unknown tool", { name: "does_not_exist", arguments: {} }],
    [
      "not_found item",
      {
        name: "item_consumption",
        arguments: { warehouse_code: "wh_001", item_id: 9_999_999 },
      },
    ],
  ];
  for (const [desc, args] of cases) {
    try {
      await mcp.call("tools/call", args);
      console.log(`  [UNEXPECTED] ${desc}: call succeeded`);
    } catch (e: any) {
      const cls = e.constructor.name;
      console.log(
        `  ${cls.padEnd(15)} ${desc.padEnd(28)} → ${e.code ?? ""} ${
          String(e.message).slice(0, 60)
        }`
      );
    }
  }

  console.log();
  console.log("-- top-level JSON-RPC errors --");
  try {
    await mcp.call("tools/list");
  } catch (e: any) {
    console.log(`  caught (re-call): ${e.message.slice(0, 60)}`);
  }

  // §1 envelope: missing Accept header (raw fetch)
  console.log();
  const res = await fetch(`${URL_.replace(/\/$/, "")}/api/mcp/`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${TOKEN_}`,
      "Content-Type": "application/json",
      // intentionally NO Accept
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
  });
  const body = await res.json();
  console.log(
    `  HTTPError      no Accept header                  → ${res.status} ${body.error.message.slice(0, 60)}`
  );

  console.log();
  console.log("Done.");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});