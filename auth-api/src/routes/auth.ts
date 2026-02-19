import { Hono } from "hono";
import type { Env, Admin } from "../types";
import { hashPassword, verifyPassword, generateJWT } from "../auth";

const authRoutes = new Hono<{ Bindings: Env }>();

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
  const adminList = await c.env.KV.get("admin:list");
  if (adminList) {
    const admins: string[] = JSON.parse(adminList);
    if (admins.length > 0) {
      return c.json(
        {
          error:
            "Registration is closed. An admin already exists. Use /auth/login instead.",
        },
        403
      );
    }
  }

  // Check if email is already taken
  const existingMapping = await c.env.KV.get(`admin:email:${email}`);
  if (existingMapping) {
    return c.json({ error: "Email already registered" }, 409);
  }

  const passwordHash = await hashPassword(body.password);
  const id = crypto.randomUUID();

  const admin: Admin = {
    id,
    email,
    passwordHash,
    createdAt: new Date().toISOString(),
  };

  // Store admin data
  await c.env.KV.put(`admin:${id}`, JSON.stringify(admin));
  // Email → admin ID mapping for login lookup
  await c.env.KV.put(`admin:email:${email}`, id);
  // Track admin list
  await c.env.KV.put("admin:list", JSON.stringify([id]));

  const token = await generateJWT(
    { sub: id, email: admin.email },
    c.env.JWT_SECRET
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
  const adminId = await c.env.KV.get(`admin:email:${email}`);
  if (!adminId) {
    return c.json({ error: "Invalid email or password" }, 401);
  }

  const adminData = await c.env.KV.get(`admin:${adminId}`);
  if (!adminData) {
    return c.json({ error: "Invalid email or password" }, 401);
  }

  const admin: Admin = JSON.parse(adminData);

  const isValid = await verifyPassword(body.password, admin.passwordHash);
  if (!isValid) {
    return c.json({ error: "Invalid email or password" }, 401);
  }

  const token = await generateJWT(
    { sub: admin.id, email: admin.email },
    c.env.JWT_SECRET
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

export default authRoutes;
