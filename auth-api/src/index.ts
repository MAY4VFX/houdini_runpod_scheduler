import { Hono } from "hono";
import { cors } from "hono/cors";
import type { Env } from "./types";
import authRoutes from "./routes/auth";
import projectRoutes from "./routes/projects";

const app = new Hono<{ Bindings: Env }>();

// --- CORS middleware ---
app.use(
  "*",
  cors({
    origin: (origin, c) => {
      const allowed = c.env.CORS_ORIGIN;
      if (allowed === "*") return "*";
      // Support comma-separated origins
      const origins = allowed.split(",").map((o: string) => o.trim());
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
    service: "runpodfarm-auth",
    timestamp: new Date().toISOString(),
  });
});

// --- Auth routes ---
app.route("/auth", authRoutes);

// --- Project routes ---
app.route("/projects", projectRoutes);

// --- 404 fallback ---
app.notFound((c) => {
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

export default app;
