#!/usr/bin/env node
/**
 * HTTP shim in front of pixel-bridge-mcp (stdio MCP server).
 *
 * The Telegram bot cannot spawn an MCP child process across containers, so the
 * engine container runs this shim: it owns the pixel-bridge MCP server as a
 * child process, translates simple REST calls into MCP tools/call requests,
 * and serves generated image files back over HTTP.
 *
 * Endpoints (all except /health require header x-api-secret === $API_SECRET):
 *   GET  /health
 *   POST /generate   {prompt, aspect_ratio?, provider?, wait_seconds?}
 *   POST /edit       {image_b64, instructions, provider?, wait_seconds?}
 *   GET  /status/:job_id?wait=N
 *   GET  /session/:provider
 *   GET  /file?path=<absolute path inside the assets dir>
 */
import http from "node:http";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import crypto from "node:crypto";

const PORT = Number(process.env.BRIDGE_PORT || 8090);
const API_SECRET = (process.env.API_SECRET || "").trim();
const MCP_CMD = process.env.PIXEL_BRIDGE_MCP_CMD || "node";
const MCP_ARGS = (process.env.PIXEL_BRIDGE_MCP_ARGS || "dist/index.js").split(" ").filter(Boolean);
const HOME = process.env.PIXEL_BRIDGE_HOME || path.join(process.env.HOME || "/tmp", ".pixel-bridge");
const ASSETS_DIR = path.join(HOME, "assets");
const UPLOADS_DIR = path.join(HOME, "uploads");

let child = null;
let connected = false;
let nextId = 1;
const pending = new Map(); // id -> {resolve, reject, timer}

function log(...args) {
  console.log(`[bridge ${new Date().toISOString()}]`, ...args);
}

const MCP_IDLE_KILL_MS = Number(process.env.MCP_IDLE_KILL_MS || 120000);
let lastUsedAt = Date.now();
let idleTimer = null;

function touchIdle() {
  lastUsedAt = Date.now();
  if (idleTimer) clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    if (child && Date.now() - lastUsedAt >= MCP_IDLE_KILL_MS) {
      log("idle timeout — stopping MCP child to free memory");
      try { child.kill("SIGTERM"); } catch {}
    }
  }, MCP_IDLE_KILL_MS + 5000);
}

function startMcp() {
  if (child && !child.killed) return;
  touchIdle();
  log("spawning MCP server:", MCP_CMD, MCP_ARGS.join(" "));
  child = spawn(MCP_CMD, MCP_ARGS, {
    cwd: "/app/pixel-bridge",
    stdio: ["pipe", "pipe", "inherit"],
    env: process.env,
  });
  child.on("exit", (code) => {
    log(`MCP server exited (code=${code}); connected=false`);
    connected = false;
    child = null;
    for (const [id, p] of pending) {
      p.reject(new Error(`MCP server exited before responding (id=${id})`));
      pending.delete(id);
    }
  });
  const rl = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  rl.on("line", (line) => {
    if (!line.trim()) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      return;
    }
    if (msg.id !== undefined && (msg.result !== undefined || msg.error !== undefined)) {
      const p = pending.get(msg.id);
      if (p) {
        clearTimeout(p.timer);
        pending.delete(msg.id);
        if (msg.error) p.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
        else p.resolve(msg.result);
      }
      return;
    }
    // Server -> client requests/notifications we don't care about.
    if (msg.method === "roots/list" && msg.id !== undefined) {
      child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result: { roots: [] } }) + "\n");
    } else if (msg.method === "sampling/createMessage" && msg.id !== undefined) {
      child.stdin.write(JSON.stringify({
        jsonrpc: "2.0", id: msg.id,
        error: { code: -32601, message: "sampling not supported by shim" },
      }) + "\n");
    } else if (msg.method === "ping" && msg.id !== undefined) {
      child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result: {} }) + "\n");
    }
  });
}

async function mcpRequest(method, params, timeoutMs = 0) {
  touchIdle();
  startMcp();
  if (!connected) {
    await initialize();
  }
  const id = nextId++;
  const payload = JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n";
  return new Promise((resolve, reject) => {
    const entry = { resolve, reject, timer: null };
    pending.set(id, entry);
    if (timeoutMs > 0) {
      entry.timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`MCP request timeout after ${timeoutMs}ms: ${method}`));
      }, timeoutMs);
    }
    child.stdin.write(payload);
  });
}

async function initialize() {
  const id = nextId++;
  const result = await new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject, timer: setTimeout(() => {
      pending.delete(id);
      reject(new Error("MCP initialize timeout"));
    }, 30000) });
    child.stdin.write(JSON.stringify({
      jsonrpc: "2.0", id,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "pixel-bridge-http-shim", version: "1.0.0" },
      },
    }) + "\n");
  });
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n");
  connected = true;
  log("MCP session initialized:", JSON.stringify(result?.serverInfo || {}));
}

async function callTool(name, args, timeoutMs = 0) {
  const result = await mcpRequest("tools/call", { name, arguments: args }, timeoutMs);
  const text = result?.content?.find((c) => c.type === "text")?.text || "";
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = { raw: text };
  }
  if (result?.isError) {
    throw new McpToolError(parsed);
  }
  return parsed;
}

class McpToolError extends Error {
  constructor(payload) {
    super(payload?.error || "MCP tool call failed");
    this.payload = payload;
  }
}

function authorized(req) {
  if (!API_SECRET) return true;
  return (req.headers["x-api-secret"] || "") === API_SECRET;
}

function readBody(req, limitMb = 40) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > limitMb * 1024 * 1024) {
        reject(new Error("body too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

async function readJson(req) {
  const buf = await readBody(req);
  if (!buf.length) return {};
  return JSON.parse(buf.toString("utf8"));
}

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  res.end(body);
}

const MIME = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif" };

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  try {
    if (url.pathname === "/health") {
      return sendJson(res, 200, { ok: true, mcp_connected: connected, assets_dir: ASSETS_DIR });
    }
    if (!authorized(req)) return sendJson(res, 401, { error: "unauthorized" });

    if (url.pathname === "/generate" && req.method === "POST") {
      const body = await readJson(req);
      if (!body.prompt) return sendJson(res, 400, { error: "prompt required" });
      const args = {
        provider: body.provider || "chatgpt",
        prompt: String(body.prompt),
        output_path: ASSETS_DIR,
        wait_seconds: Math.min(Math.max(Number(body.wait_seconds || 150), 5), 590),
      };
      if (body.aspect_ratio) args.aspect_ratio = body.aspect_ratio;
      const job = await callTool("generate_image", args);
      return sendJson(res, 200, job);
    }

    if (url.pathname === "/edit" && req.method === "POST") {
      const body = await readJson(req);
      if (!body.image_b64 || !body.instructions) {
        return sendJson(res, 400, { error: "image_b64 and instructions required" });
      }
      fs.mkdirSync(UPLOADS_DIR, { recursive: true });
      const uploadPath = path.join(UPLOADS_DIR, `upload-${Date.now()}-${crypto.randomUUID().slice(0, 8)}.png`);
      fs.writeFileSync(uploadPath, Buffer.from(body.image_b64, "base64"));
      const args = {
        provider: body.provider || "chatgpt",
        input_image_path: uploadPath,
        instructions: String(body.instructions),
        output_path: ASSETS_DIR,
        wait_seconds: Math.min(Math.max(Number(body.wait_seconds || 150), 5), 590),
      };
      const job = await callTool("edit_image", args);
      return sendJson(res, 200, job);
    }

    if (url.pathname.startsWith("/status/") && req.method === "GET") {
      const jobId = url.pathname.split("/status/")[1];
      const wait = Math.min(Math.max(Number(url.searchParams.get("wait") || 0), 0), 590);
      const job = await callTool("get_generation_status", { job_id: jobId, wait_seconds: wait });
      return sendJson(res, 200, job);
    }

    if (url.pathname.startsWith("/session/") && req.method === "GET") {
      const provider = url.pathname.split("/session/")[1] || "chatgpt";
      const status = await callTool("check_provider_session", { provider });
      return sendJson(res, 200, status);
    }

    if (url.pathname === "/file" && req.method === "GET") {
      const p = url.searchParams.get("path") || "";
      const resolved = path.resolve(p);
      if (!resolved.startsWith(path.resolve(ASSETS_DIR))) {
        return sendJson(res, 403, { error: "path outside assets dir" });
      }
      if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) {
        return sendJson(res, 404, { error: "file not found" });
      }
      const ext = path.extname(resolved).toLowerCase();
      res.writeHead(200, { "content-type": MIME[ext] || "application/octet-stream" });
      fs.createReadStream(resolved).pipe(res);
      return;
    }

    sendJson(res, 404, { error: "not found" });
  } catch (err) {
    log("error:", err.message);
    if (err instanceof McpToolError) {
      return sendJson(res, 200, err.payload);
    }
    sendJson(res, 500, { error: err.message });
  }
});

fs.mkdirSync(ASSETS_DIR, { recursive: true });
fs.mkdirSync(UPLOADS_DIR, { recursive: true });
server.listen(PORT, "127.0.0.1", () => log(`http shim listening on 127.0.0.1:${PORT}`));
