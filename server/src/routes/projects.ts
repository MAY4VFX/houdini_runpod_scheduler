import { Hono } from "hono";
import { randomUUID } from "node:crypto";
import type { Project, Artist, JuiceFSConfig } from "../types.js";
import { authMiddleware, generateApiKey } from "../auth.js";
import type { Store } from "../db.js";

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

export function createProjectRoutes(store: Store, jwtSecret: string) {
  const projectRoutes = new Hono<{
    Variables: { adminId: string; email: string };
  }>();

  // --- Protected routes (require JWT) ---

  /**
   * POST /projects
   * Create a new project.
   * Body: { name, redisUrl, b2Endpoint, b2AccessKey, b2SecretKey, b2Bucket, juicefsRsaKey }
   */
  projectRoutes.post("/", authMiddleware(jwtSecret), async (c) => {
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
      if (
        !body[field] ||
        typeof body[field] !== "string" ||
        !body[field].trim()
      ) {
        return c.json({ error: `Field '${field}' is required` }, 400);
      }
    }

    const id = randomUUID();

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

    store.createProject(project);

    return c.json({ project: sanitizeProject(project) }, 201);
  });

  /**
   * GET /projects
   * List all projects for the authenticated admin.
   */
  projectRoutes.get("/", authMiddleware(jwtSecret), async (c) => {
    const adminId = c.get("adminId");

    const projects = store.getProjectsByAdmin(adminId);

    return c.json({ projects: projects.map(sanitizeProject) });
  });

  /**
   * POST /projects/:id/artists
   * Add an artist to a project. Generates an API key.
   * Body: { name, email }
   */
  projectRoutes.post(
    "/:id/artists",
    authMiddleware(jwtSecret),
    async (c) => {
      const adminId = c.get("adminId");
      const projectId = c.req.param("id");

      // Verify project exists and belongs to this admin
      const project = store.getProject(projectId);
      if (!project) {
        return c.json({ error: "Project not found" }, 404);
      }

      if (project.adminId !== adminId) {
        return c.json({ error: "Access denied" }, 403);
      }

      const body = await c.req.json<{ name?: string; email?: string }>();

      if (
        !body.name ||
        typeof body.name !== "string" ||
        !body.name.trim()
      ) {
        return c.json({ error: "Field 'name' is required" }, 400);
      }

      if (
        !body.email ||
        typeof body.email !== "string" ||
        !body.email.trim()
      ) {
        return c.json({ error: "Field 'email' is required" }, 400);
      }

      const email = body.email.trim().toLowerCase();
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        return c.json({ error: "Invalid email format" }, 400);
      }

      const id = randomUUID();
      const apiKey = generateApiKey();

      const artist: Artist = {
        id,
        name: body.name.trim(),
        email,
        apiKey,
        projectId,
        createdAt: new Date().toISOString(),
      };

      store.createArtist(artist);

      return c.json({ artist }, 201);
    }
  );

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
    const artist = store.getArtistByApiKey(apiKey);
    if (!artist) {
      return c.json({ error: "Invalid API key" }, 401);
    }

    // Verify the API key belongs to the requested project
    if (artist.projectId !== projectId) {
      return c.json(
        { error: "API key does not belong to this project" },
        403
      );
    }

    // Verify artist is not revoked
    if (artist.revokedAt) {
      return c.json({ error: "API key has been revoked" }, 403);
    }

    // Get project data
    const project = store.getProject(projectId);
    if (!project) {
      return c.json({ error: "Project not found" }, 404);
    }

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
    authMiddleware(jwtSecret),
    async (c) => {
      const adminId = c.get("adminId");
      const projectId = c.req.param("id");
      const artistId = c.req.param("artistId");

      // Verify project exists and belongs to this admin
      const project = store.getProject(projectId);
      if (!project) {
        return c.json({ error: "Project not found" }, 404);
      }

      if (project.adminId !== adminId) {
        return c.json({ error: "Access denied" }, 403);
      }

      // Get artist
      const artist = store.getArtist(artistId);
      if (!artist) {
        return c.json({ error: "Artist not found" }, 404);
      }

      if (artist.projectId !== projectId) {
        return c.json(
          { error: "Artist does not belong to this project" },
          400
        );
      }

      if (artist.revokedAt) {
        return c.json({ error: "Artist access already revoked" }, 409);
      }

      // Revoke: set revokedAt timestamp
      store.revokeArtist(artistId);

      // Reload to get the updated revokedAt value
      const updatedArtist = store.getArtist(artistId);

      return c.json({
        message: "Artist access revoked",
        artist: {
          id: artist.id,
          name: artist.name,
          email: artist.email,
          revokedAt: updatedArtist?.revokedAt,
        },
      });
    }
  );

  /**
   * GET /projects/:id/artists
   * List all artists for a project.
   */
  projectRoutes.get(
    "/:id/artists",
    authMiddleware(jwtSecret),
    async (c) => {
      const adminId = c.get("adminId");
      const projectId = c.req.param("id");

      // Verify project exists and belongs to this admin
      const project = store.getProject(projectId);
      if (!project) {
        return c.json({ error: "Project not found" }, 404);
      }

      if (project.adminId !== adminId) {
        return c.json({ error: "Access denied" }, 403);
      }

      const artists = store.listArtists(projectId);

      // Strip apiKey from list response for security
      const safeArtists = artists.map(({ apiKey, ...safe }) => safe);

      return c.json({ artists: safeArtists });
    }
  );

  return projectRoutes;
}
