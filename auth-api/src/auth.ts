import { Context, Next } from "hono";
import { SignJWT, jwtVerify } from "jose";
import type { Env } from "./types";

const PBKDF2_ITERATIONS = 100_000;
const SALT_LENGTH = 16;
const KEY_LENGTH = 32;

/**
 * Hash a password using PBKDF2 with the Web Crypto API.
 * Returns a string in the format: base64(salt):base64(derivedKey)
 */
export async function hashPassword(password: string): Promise<string> {
  const salt = crypto.getRandomValues(new Uint8Array(SALT_LENGTH));

  const keyMaterial = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"]
  );

  const derivedBits = await crypto.subtle.deriveBits(
    {
      name: "PBKDF2",
      salt,
      iterations: PBKDF2_ITERATIONS,
      hash: "SHA-256",
    },
    keyMaterial,
    KEY_LENGTH * 8
  );

  const saltB64 = btoa(String.fromCharCode(...salt));
  const hashB64 = btoa(String.fromCharCode(...new Uint8Array(derivedBits)));

  return `${saltB64}:${hashB64}`;
}

/**
 * Verify a password against a stored hash.
 */
export async function verifyPassword(
  password: string,
  storedHash: string
): Promise<boolean> {
  const [saltB64, expectedHashB64] = storedHash.split(":");
  if (!saltB64 || !expectedHashB64) {
    return false;
  }

  const salt = Uint8Array.from(atob(saltB64), (c) => c.charCodeAt(0));

  const keyMaterial = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"]
  );

  const derivedBits = await crypto.subtle.deriveBits(
    {
      name: "PBKDF2",
      salt,
      iterations: PBKDF2_ITERATIONS,
      hash: "SHA-256",
    },
    keyMaterial,
    KEY_LENGTH * 8
  );

  const actualHashB64 = btoa(
    String.fromCharCode(...new Uint8Array(derivedBits))
  );

  return actualHashB64 === expectedHashB64;
}

/**
 * Generate a JWT token with the given payload.
 * Token expires in 24 hours.
 */
export async function generateJWT(
  payload: Record<string, unknown>,
  secret: string
): Promise<string> {
  const secretKey = new TextEncoder().encode(secret);

  return new SignJWT(payload)
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setExpirationTime("24h")
    .sign(secretKey);
}

/**
 * Verify and decode a JWT token.
 * Returns the payload if valid, throws otherwise.
 */
export async function verifyJWT(
  token: string,
  secret: string
): Promise<Record<string, unknown>> {
  const secretKey = new TextEncoder().encode(secret);

  const { payload } = await jwtVerify(token, secretKey, {
    algorithms: ["HS256"],
  });

  return payload as Record<string, unknown>;
}

/**
 * Generate an API key for artists.
 * Format: rpf_{uuid}
 */
export function generateApiKey(): string {
  return `rpf_${crypto.randomUUID()}`;
}

/**
 * Hono middleware that verifies JWT from the Authorization header.
 * Sets `adminId` and `email` on the context variable.
 */
export function authMiddleware(env: Env) {
  return async (c: Context, next: Next) => {
    const authHeader = c.req.header("Authorization");
    if (!authHeader) {
      return c.json({ error: "Authorization header is required" }, 401);
    }

    const match = authHeader.match(/^Bearer\s+(.+)$/i);
    if (!match) {
      return c.json(
        { error: "Authorization header must use Bearer scheme" },
        401
      );
    }

    const token = match[1];

    try {
      const payload = await verifyJWT(token, env.JWT_SECRET);
      c.set("adminId", payload.sub as string);
      c.set("email", payload.email as string);
    } catch {
      return c.json({ error: "Invalid or expired token" }, 401);
    }

    await next();
  };
}
