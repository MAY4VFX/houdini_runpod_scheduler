export interface Admin {
  id: string;
  email: string;
  passwordHash: string;
  createdAt: string;
}

export interface Project {
  id: string;
  name: string;
  adminId: string;
  redisUrl: string;
  b2Endpoint: string;
  b2AccessKey: string;
  b2SecretKey: string;
  b2Bucket: string;
  juicefsRsaKey: string;
  createdAt: string;
}

export interface Artist {
  id: string;
  name: string;
  email: string;
  apiKey: string;
  projectId: string;
  createdAt: string;
  revokedAt?: string;
}

export interface JuiceFSConfig {
  redisUrl: string;
  b2Endpoint: string;
  b2AccessKey: string;
  b2SecretKey: string;
  b2Bucket: string;
  rsaKey: string;
  projectId: string;
  mountPath: string;
}

/**
 * Environment variables for the server.
 */
export interface ServerEnv {
  JWT_SECRET: string;
  CORS_ORIGIN: string;
  PORT: string;
  DATABASE_PATH: string;
  REDIS_URL: string;
}
