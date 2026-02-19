import { Hono } from "hono";
import { randomUUID } from "node:crypto";
import type { Admin } from "../types.js";
import { hashPassword, verifyPassword, generateJWT } from "../auth.js";
import type { Store } from "../db.js";

export function createAuthRoutes(store: Store, jwtSecret: string) {
  const authRoutes = new Hono();

  /**
   * POST /auth/register
   * First admin registration. Only works if no admins exist yet.
   * Body: { email, password }
   * Returns: { token, admin: { id, email, createdAt } }
   */
  authRoutes.post("/register", async (c) => {
    const body = await c.req.json<{ email?: string; password?: string }>();

    if (!body.email || !body.password) {
      return c.json({ error: "Email and password are required" }, 400);
    }

    const email = body.email.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      return c.json({ error: "Invalid email format" }, 400);
    }

    if (body.password.length < 8) {
      return c.json(
        { error: "Password must be at least 8 characters long" },
        400
      );
    }

    // Check if any admins already exist
    const adminCount = store.getAdminCount();
    if (adminCount > 0) {
      return c.json(
        {
          error:
            "Registration is closed. An admin already exists. Use /auth/login instead.",
        },
        403
      );
    }

    // Check if email is already taken
    const existingAdmin = store.getAdminByEmail(email);
    if (existingAdmin) {
      return c.json({ error: "Email already registered" }, 409);
    }

    const passwordHash = hashPassword(body.password);
    const id = randomUUID();

    const admin: Admin = {
      id,
      email,
      passwordHash,
      createdAt: new Date().toISOString(),
    };

    store.createAdmin(admin);

    const token = await generateJWT(
      { sub: id, email: admin.email },
      jwtSecret
    );

    return c.json(
      {
        token,
        admin: {
          id: admin.id,
          email: admin.email,
          createdAt: admin.createdAt,
        },
      },
      201
    );
  });

  /**
   * POST /auth/login
   * Body: { email, password }
   * Returns: { token, admin: { id, email, createdAt } }
   */
  authRoutes.post("/login", async (c) => {
    const body = await c.req.json<{ email?: string; password?: string }>();

    if (!body.email || !body.password) {
      return c.json({ error: "Email and password are required" }, 400);
    }

    const email = body.email.trim().toLowerCase();

    // Look up admin by email
    const admin = store.getAdminByEmail(email);
    if (!admin) {
      return c.json({ error: "Invalid email or password" }, 401);
    }

    const isValid = verifyPassword(body.password, admin.passwordHash);
    if (!isValid) {
      return c.json({ error: "Invalid email or password" }, 401);
    }

    const token = await generateJWT(
      { sub: admin.id, email: admin.email },
      jwtSecret
    );

    return c.json({
      token,
      admin: {
        id: admin.id,
        email: admin.email,
        createdAt: admin.createdAt,
      },
    });
  });

  return authRoutes;
}
