import { Hono } from "hono";
import Redis from "ioredis";
import { authMiddleware } from "../auth.js";
import type { Store } from "../db.js";

export function createMonitoringRoutes(
  store: Store,
  jwtSecret: string,
  redisUrl?: string
) {
  const monitoringRoutes = new Hono<{
    Variables: { adminId: string; email: string };
  }>();

  /**
   * Get a Redis client for the given project's Redis URL,
   * or the server-wide REDIS_URL if project doesn't have one.
   * Returns null if no Redis is configured.
   */
  function getRedisClient(projectRedisUrl?: string): Redis | null {
    const url = projectRedisUrl || redisUrl;
    if (!url) return null;

    return new Redis(url, {
      maxRetriesPerRequest: 1,
      connectTimeout: 5000,
      lazyConnect: true,
    });
  }

  /**
   * Helper: verify project ownership and get project data.
   */
  function getVerifiedProject(projectId: string, adminId: string) {
    const project = store.getProject(projectId);
    if (!project) return { error: "Project not found", status: 404 as const };
    if (project.adminId !== adminId)
      return { error: "Access denied", status: 403 as const };
    return { project };
  }

  /**
   * GET /api/monitoring/jobs/:projectId
   * Active render jobs from Redis.
   */
  monitoringRoutes.get(
    "/jobs/:projectId",
    authMiddleware(jwtSecret),
    async (c) => {
      const adminId = c.get("adminId");
      const projectId = c.req.param("projectId");

      const result = getVerifiedProject(projectId, adminId);
      if ("error" in result) {
        return c.json({ error: result.error }, result.status);
      }

      const redis = getRedisClient(result.project.redisUrl);
      if (!redis) {
        return c.json({ jobs: [], message: "Redis not configured" });
      }

      try {
        await redis.connect();

        // Get task queue length (pending jobs)
        const pattern = `rp:tasks:${projectId}:*`;
        const taskKeys = await redis.keys(pattern);

        // Get result keys for completed/running jobs
        const resultPattern = `rp:results:*`;
        const resultKeys = await redis.keys(resultPattern);

        const jobs: Array<{
          id: string;
          status: string;
          data: unknown;
        }> = [];

        // Fetch results
        for (const key of resultKeys) {
          const data = await redis.get(key);
          if (data) {
            try {
              const parsed = JSON.parse(data);
              if (parsed.projectId === projectId) {
                jobs.push({
                  id: key.replace("rp:results:", ""),
                  status: parsed.status || "unknown",
                  data: parsed,
                });
              }
            } catch {
              // Skip malformed data
            }
          }
        }

        return c.json({
          jobs,
          pendingQueues: taskKeys.length,
        });
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Redis connection failed";
        return c.json({ error: message, jobs: [] }, 502);
      } finally {
        redis.disconnect();
      }
    }
  );

  /**
   * GET /api/monitoring/pods/:projectId
   * Active pods and their status from Redis heartbeats.
   */
  monitoringRoutes.get(
    "/pods/:projectId",
    authMiddleware(jwtSecret),
    async (c) => {
      const adminId = c.get("adminId");
      const projectId = c.req.param("projectId");

      const result = getVerifiedProject(projectId, adminId);
      if ("error" in result) {
        return c.json({ error: result.error }, result.status);
      }

      const redis = getRedisClient(result.project.redisUrl);
      if (!redis) {
        return c.json({ pods: [], message: "Redis not configured" });
      }

      try {
        await redis.connect();

        // Get pod heartbeats
        const heartbeatPattern = `rp:heartbeat:*`;
        const heartbeatKeys = await redis.keys(heartbeatPattern);

        // Get pod registry
        const podPattern = `rp:pods:${projectId}:*`;
        const podKeys = await redis.keys(podPattern);

        const pods: Array<{
          id: string;
          alive: boolean;
          data: unknown;
        }> = [];

        // Fetch pod registry data
        for (const key of podKeys) {
          const data = await redis.get(key);
          if (data) {
            try {
              const parsed = JSON.parse(data);
              const podId = parsed.podId || key.split(":").pop();

              // Check if heartbeat exists (pod is alive)
              const heartbeatExists = await redis.exists(
                `rp:heartbeat:${podId}`
              );

              pods.push({
                id: podId,
                alive: heartbeatExists === 1,
                data: parsed,
              });
            } catch {
              // Skip malformed data
            }
          }
        }

        return c.json({
          pods,
          totalHeartbeats: heartbeatKeys.length,
        });
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Redis connection failed";
        return c.json({ error: message, pods: [] }, 502);
      } finally {
        redis.disconnect();
      }
    }
  );

  /**
   * GET /api/monitoring/costs/:projectId
   * Cost tracking from Redis metrics.
   */
  monitoringRoutes.get(
    "/costs/:projectId",
    authMiddleware(jwtSecret),
    async (c) => {
      const adminId = c.get("adminId");
      const projectId = c.req.param("projectId");

      const result = getVerifiedProject(projectId, adminId);
      if ("error" in result) {
        return c.json({ error: result.error }, result.status);
      }

      const redis = getRedisClient(result.project.redisUrl);
      if (!redis) {
        return c.json({
          costs: null,
          message: "Redis not configured",
        });
      }

      try {
        await redis.connect();

        // Get metrics keys for this project
        const metricsPattern = `rp:metrics:${projectId}:*`;
        const metricsKeys = await redis.keys(metricsPattern);

        const metrics: Record<string, unknown> = {};

        for (const key of metricsKeys) {
          const data = await redis.get(key);
          if (data) {
            const metricName = key.replace(`rp:metrics:${projectId}:`, "");
            try {
              metrics[metricName] = JSON.parse(data);
            } catch {
              metrics[metricName] = data;
            }
          }
        }

        return c.json({ costs: metrics });
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Redis connection failed";
        return c.json({ error: message, costs: null }, 502);
      } finally {
        redis.disconnect();
      }
    }
  );

  /**
   * GET /api/monitoring/logs/:taskId
   * Task logs from Redis.
   */
  monitoringRoutes.get(
    "/logs/:taskId",
    authMiddleware(jwtSecret),
    async (c) => {
      const taskId = c.req.param("taskId");

      // For logs, we use the server-wide REDIS_URL since we don't know the project
      const redis = getRedisClient();
      if (!redis) {
        return c.json({ logs: [], message: "Redis not configured" });
      }

      try {
        await redis.connect();

        const logKey = `rp:logs:${taskId}`;
        const logs = await redis.lrange(logKey, 0, -1);

        return c.json({
          taskId,
          logs: logs.map((log) => {
            try {
              return JSON.parse(log);
            } catch {
              return { message: log };
            }
          }),
        });
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Redis connection failed";
        return c.json({ error: message, logs: [] }, 502);
      } finally {
        redis.disconnect();
      }
    }
  );

  return monitoringRoutes;
}
