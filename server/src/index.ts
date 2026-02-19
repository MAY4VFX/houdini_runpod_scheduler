import { serve } from "@hono/node-server";
import { serveStatic } from "@hono/node-server/serve-static";
import { Hono } from "hono";
import { cors } from "hono/cors";
import { Store } from "./db.js";
import { createAuthRoutes } from "./routes/auth.js";
import { createProjectRoutes } from "./routes/projects.js";
import { createMonitoringRoutes } from "./routes/monitoring.js";

// --- Configuration from environment ---
const port = parseInt(process.env.PORT || "3000");
const jwtSecret = process.env.JWT_SECRET || "dev-secret-change-in-production";
const corsOrigin = process.env.CORS_ORIGIN || "*";
const databasePath = process.env.DATABASE_PATH || "./data/runpodfarm.db";
const redisUrl = process.env.REDIS_URL || "";

if (jwtSecret === "dev-secret-change-in-production") {
  console.warn(
    "WARNING: Using default JWT secret. Set JWT_SECRET environment variable for production."
  );
}

// --- Initialize store ---
const store = new Store(databasePath);

// --- Create Hono app ---
const app = new Hono();

// --- CORS middleware ---
app.use(
  "*",
  cors({
    origin: (origin) => {
      if (corsOrigin === "*") return "*";
      const origins = corsOrigin.split(",").map((o: string) => o.trim());
      if (origins.includes(origin)) return origin;
      return "";
    },
    allowMethods: ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allowHeaders: ["Content-Type", "Authorization", "X-API-Key"],
    exposeHeaders: ["Content-Length"],
    maxAge: 86400,
    credentials: true,
  })
);

// --- Health check ---
app.get("/health", (c) => {
  return c.json({
    status: "ok",
    service: "runpodfarm-server",
    timestamp: new Date().toISOString(),
  });
});

// --- API routes ---
app.route("/api/auth", createAuthRoutes(store, jwtSecret));
app.route("/api/projects", createProjectRoutes(store, jwtSecret));
app.route("/api/monitoring", createMonitoringRoutes(store, jwtSecret, redisUrl));

// --- Backward-compatible routes (without /api prefix) ---
app.route("/auth", createAuthRoutes(store, jwtSecret));
app.route("/projects", createProjectRoutes(store, jwtSecret));

// --- Serve dashboard static files ---
// Static assets (JS, CSS, images, etc.)
app.use("/assets/*", serveStatic({ root: "./public" }));

// Fallback: serve index.html for SPA routing (only for non-API paths)
app.get("*", serveStatic({ root: "./public", path: "index.html" }));

// --- 404 fallback for API routes ---
app.notFound((c) => {
  // If the request is for an API path, return JSON 404
  if (
    c.req.path.startsWith("/api/") ||
    c.req.path.startsWith("/auth/") ||
    c.req.path.startsWith("/projects/")
  ) {
    return c.json({ error: "Not found" }, 404);
  }
  // Otherwise it was a static file miss handled by serveStatic above
  return c.json({ error: "Not found" }, 404);
});

// --- Global error handler ---
app.onError((err, c) => {
  console.error(`Unhandled error: ${err.message}`, err.stack);

  if (err instanceof SyntaxError) {
    return c.json({ error: "Invalid JSON in request body" }, 400);
  }

  return c.json({ error: "Internal server error" }, 500);
});

// --- Start server ---
serve({ fetch: app.fetch, port });
console.log(`RunPodFarm Server running on port ${port}`);

// --- Graceful shutdown ---
function shutdown() {
  console.log("Shutting down...");
  store.close();
  process.exit(0);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
