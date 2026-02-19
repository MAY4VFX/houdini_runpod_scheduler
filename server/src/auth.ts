import { Context, Next } from "hono";
import { SignJWT, jwtVerify } from "jose";
import { randomBytes, pbkdf2Sync, randomUUID } from "node:crypto";

const PBKDF2_ITERATIONS = 100_000;
const SALT_LENGTH = 16;
const KEY_LENGTH = 32;

/**
 * Hash a password using PBKDF2 with Node.js crypto.
 * Returns a string in the format: base64(salt):base64(derivedKey)
 */
export function hashPassword(password: string): string {
  const salt = randomBytes(SALT_LENGTH);

  const derivedKey = pbkdf2Sync(
    password,
    salt,
    PBKDF2_ITERATIONS,
    KEY_LENGTH,
    "sha256"
  );

  const saltB64 = salt.toString("base64");
  const hashB64 = derivedKey.toString("base64");

  return `${saltB64}:${hashB64}`;
}

/**
 * Verify a password against a stored hash.
 */
export function verifyPassword(
  password: string,
  storedHash: string
): boolean {
  const [saltB64, expectedHashB64] = storedHash.split(":");
  if (!saltB64 || !expectedHashB64) {
    return false;
  }

  const salt = Buffer.from(saltB64, "base64");

  const derivedKey = pbkdf2Sync(
    password,
    salt,
    PBKDF2_ITERATIONS,
    KEY_LENGTH,
    "sha256"
  );

  const actualHashB64 = derivedKey.toString("base64");

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
  return `rpf_${randomUUID()}`;
}

/**
 * Hono middleware that verifies JWT from the Authorization header.
 * Sets `adminId` and `email` on the context variable.
 */
export function authMiddleware(jwtSecret: string) {
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
      const payload = await verifyJWT(token, jwtSecret);
      c.set("adminId", payload.sub as string);
      c.set("email", payload.email as string);
    } catch {
      return c.json({ error: "Invalid or expired token" }, 401);
    }

    await next();
  };
}
