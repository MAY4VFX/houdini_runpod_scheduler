import { Hono } from "hono";
import type { Env, Project, Artist, JuiceFSConfig } from "../types";
import { authMiddleware, generateApiKey } from "../auth";

const projectRoutes = new Hono<{
  Bindings: Env;
  Variables: { adminId: string; email: string };
}>();

/**
 * Sanitize a project for API responses (strip sensitive fields).
 */
function sanitizeProject(
  project: Project
): Omit<
  Project,
  "redisUrl" | "b2AccessKey" | "b2SecretKey" | "juicefsRsaKey"
> {
  const { redisUrl, b2AccessKey, b2SecretKey, juicefsRsaKey, ...safe } =
    project;
  return safe;
}

// --- Protected routes (require JWT) ---

/**
 * POST /projects
 * Create a new project.
 * Body: { name, redisUrl, b2Endpoint, b2AccessKey, b2SecretKey, b2Bucket, juicefsRsaKey }
 */
projectRoutes.post("/", authMiddleware as any, async (c) => {
  const adminId = c.get("adminId");

  const body = await c.req.json<{
    name?: string;
    redisUrl?: string;
    b2Endpoint?: string;
    b2AccessKey?: string;
    b2SecretKey?: string;
    b2Bucket?: string;
    juicefsRsaKey?: string;
  }>();

  // Validate required fields
  const requiredFields = [
    "name",
    "redisUrl",
    "b2Endpoint",
    "b2AccessKey",
    "b2SecretKey",
    "b2Bucket",
    "juicefsRsaKey",
  ] as const;

  for (const field of requiredFields) {
    if (!body[field] || typeof body[field] !== "string" || !body[field].trim()) {
      return c.json({ error: `Field '${field}' is required` }, 400);
    }
  }

  const id = crypto.randomUUID();

  const project: Project = {
    id,
    name: body.name!.trim(),
    adminId,
    redisUrl: body.redisUrl!.trim(),
    b2Endpoint: body.b2Endpoint!.trim(),
    b2AccessKey: body.b2AccessKey!.trim(),
    b2SecretKey: body.b2SecretKey!.trim(),
    b2Bucket: body.b2Bucket!.trim(),
    juicefsRsaKey: body.juicefsRsaKey!.trim(),
    createdAt: new Date().toISOString(),
  };

  // Store the project
  await c.env.KV.put(`project:${id}`, JSON.stringify(project));

  // Add to admin's project list
  const projectListKey = `admin:${adminId}:projects`;
  const existingList = await c.env.KV.get(projectListKey);
  const projectIds: string[] = existingList ? JSON.parse(existingList) : [];
  projectIds.push(id);
  await c.env.KV.put(projectListKey, JSON.stringify(projectIds));

  return c.json({ project: sanitizeProject(project) }, 201);
});

/**
 * GET /projects
 * List all projects for the authenticated admin.
 */
projectRoutes.get("/", authMiddleware as any, async (c) => {
  const adminId = c.get("adminId");

  const projectListKey = `admin:${adminId}:projects`;
  const existingList = await c.env.KV.get(projectListKey);
  const projectIds: string[] = existingList ? JSON.parse(existingList) : [];

  const projects: ReturnType<typeof sanitizeProject>[] = [];

  for (const pid of projectIds) {
    const data = await c.env.KV.get(`project:${pid}`);
    if (data) {
      const project: Project = JSON.parse(data);
      projects.push(sanitizeProject(project));
    }
  }

  return c.json({ projects });
});

/**
 * POST /projects/:id/artists
 * Add an artist to a project. Generates an API key.
 * Body: { name, email }
 */
projectRoutes.post("/:id/artists", authMiddleware as any, async (c) => {
  const adminId = c.get("adminId");
  const projectId = c.req.param("id");

  // Verify project exists and belongs to this admin
  const projectData = await c.env.KV.get(`project:${projectId}`);
  if (!projectData) {
    return c.json({ error: "Project not found" }, 404);
  }

  const project: Project = JSON.parse(projectData);
  if (project.adminId !== adminId) {
    return c.json({ error: "Access denied" }, 403);
  }

  const body = await c.req.json<{ name?: string; email?: string }>();

  if (!body.name || typeof body.name !== "string" || !body.name.trim()) {
    return c.json({ error: "Field 'name' is required" }, 400);
  }

  if (!body.email || typeof body.email !== "string" || !body.email.trim()) {
    return c.json({ error: "Field 'email' is required" }, 400);
  }

  const email = body.email.trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return c.json({ error: "Invalid email format" }, 400);
  }

  const id = crypto.randomUUID();
  const apiKey = generateApiKey();

  const artist: Artist = {
    id,
    name: body.name.trim(),
    email,
    apiKey,
    projectId,
    createdAt: new Date().toISOString(),
  };

  // Store artist data
  await c.env.KV.put(`artist:${id}`, JSON.stringify(artist));

  // API key → artist ID mapping for config lookup
  await c.env.KV.put(`apikey:${apiKey}`, JSON.stringify({ artistId: id, projectId }));

  // Add to project's artist list
  const artistListKey = `project:${projectId}:artists`;
  const existingList = await c.env.KV.get(artistListKey);
  const artistIds: string[] = existingList ? JSON.parse(existingList) : [];
  artistIds.push(id);
  await c.env.KV.put(artistListKey, JSON.stringify(artistIds));

  return c.json({ artist }, 201);
});

/**
 * GET /projects/:id/config
 * Get JuiceFS config by API key (for desktop app).
 * Requires X-API-Key header instead of JWT.
 */
projectRoutes.get("/:id/config", async (c) => {
  const projectId = c.req.param("id");
  const apiKey = c.req.header("X-API-Key");

  if (!apiKey) {
    return c.json({ error: "X-API-Key header is required" }, 401);
  }

  // Look up artist by API key
  const apiKeyData = await c.env.KV.get(`apikey:${apiKey}`);
  if (!apiKeyData) {
    return c.json({ error: "Invalid API key" }, 401);
  }

  const { artistId, projectId: mappedProjectId } = JSON.parse(apiKeyData) as {
    artistId: string;
    projectId: string;
  };

  // Verify the API key belongs to the requested project
  if (mappedProjectId !== projectId) {
    return c.json(
      { error: "API key does not belong to this project" },
      403
    );
  }

  // Verify artist is not revoked
  const artistData = await c.env.KV.get(`artist:${artistId}`);
  if (!artistData) {
    return c.json({ error: "Artist not found" }, 404);
  }

  const artist: Artist = JSON.parse(artistData);
  if (artist.revokedAt) {
    return c.json({ error: "API key has been revoked" }, 403);
  }

  // Get project data
  const projectData = await c.env.KV.get(`project:${projectId}`);
  if (!projectData) {
    return c.json({ error: "Project not found" }, 404);
  }

  const project: Project = JSON.parse(projectData);

  const config: JuiceFSConfig = {
    redisUrl: project.redisUrl,
    b2Endpoint: project.b2Endpoint,
    b2AccessKey: project.b2AccessKey,
    b2SecretKey: project.b2SecretKey,
    b2Bucket: project.b2Bucket,
    rsaKey: project.juicefsRsaKey,
    projectId: project.id,
    mountPath: `/mnt/juicefs/${project.id}`,
  };

  return c.json({ config });
});

/**
 * DELETE /projects/:id/artists/:artistId
 * Revoke an artist's access.
 */
projectRoutes.delete(
  "/:id/artists/:artistId",
  authMiddleware as any,
  async (c) => {
    const adminId = c.get("adminId");
    const projectId = c.req.param("id");
    const artistId = c.req.param("artistId");

    // Verify project exists and belongs to this admin
    const projectData = await c.env.KV.get(`project:${projectId}`);
    if (!projectData) {
      return c.json({ error: "Project not found" }, 404);
    }

    const project: Project = JSON.parse(projectData);
    if (project.adminId !== adminId) {
      return c.json({ error: "Access denied" }, 403);
    }

    // Get artist
    const artistData = await c.env.KV.get(`artist:${artistId}`);
    if (!artistData) {
      return c.json({ error: "Artist not found" }, 404);
    }

    const artist: Artist = JSON.parse(artistData);

    if (artist.projectId !== projectId) {
      return c.json({ error: "Artist does not belong to this project" }, 400);
    }

    if (artist.revokedAt) {
      return c.json({ error: "Artist access already revoked" }, 409);
    }

    // Revoke: set revokedAt timestamp
    artist.revokedAt = new Date().toISOString();
    await c.env.KV.put(`artist:${artistId}`, JSON.stringify(artist));

    // Delete the API key mapping so it can no longer be used
    await c.env.KV.delete(`apikey:${artist.apiKey}`);

    return c.json({
      message: "Artist access revoked",
      artist: {
        id: artist.id,
        name: artist.name,
        email: artist.email,
        revokedAt: artist.revokedAt,
      },
    });
  }
);

/**
 * GET /projects/:id/artists
 * List all artists for a project.
 */
projectRoutes.get("/:id/artists", authMiddleware as any, async (c) => {
  const adminId = c.get("adminId");
  const projectId = c.req.param("id");

  // Verify project exists and belongs to this admin
  const projectData = await c.env.KV.get(`project:${projectId}`);
  if (!projectData) {
    return c.json({ error: "Project not found" }, 404);
  }

  const project: Project = JSON.parse(projectData);
  if (project.adminId !== adminId) {
    return c.json({ error: "Access denied" }, 403);
  }

  const artistListKey = `project:${projectId}:artists`;
  const existingList = await c.env.KV.get(artistListKey);
  const artistIds: string[] = existingList ? JSON.parse(existingList) : [];

  const artists: Omit<Artist, "apiKey">[] = [];

  for (const aid of artistIds) {
    const data = await c.env.KV.get(`artist:${aid}`);
    if (data) {
      const artist: Artist = JSON.parse(data);
      // Strip apiKey from list response for security
      const { apiKey, ...safe } = artist;
      artists.push(safe);
    }
  }

  return c.json({ artists });
});

export default projectRoutes;
