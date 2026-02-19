import Database from "better-sqlite3";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import type { Admin, Project, Artist } from "./types.js";

export class Store {
  private db: Database.Database;

  constructor(dbPath: string = "./data/runpodfarm.db") {
    // Ensure the data directory exists
    mkdirSync(dirname(dbPath), { recursive: true });

    this.db = new Database(dbPath);

    // Enable WAL mode for better concurrent read performance
    this.db.pragma("journal_mode = WAL");
    this.db.pragma("foreign_keys = ON");

    this.runMigrations();
  }

  private runMigrations(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS admins (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        admin_id TEXT NOT NULL,
        redis_url TEXT NOT NULL,
        b2_endpoint TEXT NOT NULL,
        b2_access_key TEXT NOT NULL,
        b2_secret_key TEXT NOT NULL,
        b2_bucket TEXT NOT NULL,
        juicefs_rsa_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (admin_id) REFERENCES admins(id)
      );

      CREATE TABLE IF NOT EXISTS artists (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        api_key TEXT NOT NULL UNIQUE,
        project_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        FOREIGN KEY (project_id) REFERENCES projects(id)
      );

      CREATE INDEX IF NOT EXISTS idx_admins_email ON admins(email);
      CREATE INDEX IF NOT EXISTS idx_projects_admin_id ON projects(admin_id);
      CREATE INDEX IF NOT EXISTS idx_artists_project_id ON artists(project_id);
      CREATE INDEX IF NOT EXISTS idx_artists_api_key ON artists(api_key);
    `);
  }

  // --- Admin methods ---

  getAdmin(id: string): Admin | null {
    const row = this.db
      .prepare("SELECT id, email, password_hash, created_at FROM admins WHERE id = ?")
      .get(id) as { id: string; email: string; password_hash: string; created_at: string } | undefined;

    if (!row) return null;
    return {
      id: row.id,
      email: row.email,
      passwordHash: row.password_hash,
      createdAt: row.created_at,
    };
  }

  getAdminByEmail(email: string): Admin | null {
    const row = this.db
      .prepare("SELECT id, email, password_hash, created_at FROM admins WHERE email = ?")
      .get(email) as { id: string; email: string; password_hash: string; created_at: string } | undefined;

    if (!row) return null;
    return {
      id: row.id,
      email: row.email,
      passwordHash: row.password_hash,
      createdAt: row.created_at,
    };
  }

  getAdminCount(): number {
    const row = this.db
      .prepare("SELECT COUNT(*) as count FROM admins")
      .get() as { count: number };
    return row.count;
  }

  createAdmin(admin: Admin): void {
    this.db
      .prepare("INSERT INTO admins (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)")
      .run(admin.id, admin.email, admin.passwordHash, admin.createdAt);
  }

  // --- Project methods ---

  getProject(id: string): Project | null {
    const row = this.db
      .prepare(
        "SELECT id, name, admin_id, redis_url, b2_endpoint, b2_access_key, b2_secret_key, b2_bucket, juicefs_rsa_key, created_at FROM projects WHERE id = ?"
      )
      .get(id) as {
        id: string;
        name: string;
        admin_id: string;
        redis_url: string;
        b2_endpoint: string;
        b2_access_key: string;
        b2_secret_key: string;
        b2_bucket: string;
        juicefs_rsa_key: string;
        created_at: string;
      } | undefined;

    if (!row) return null;
    return {
      id: row.id,
      name: row.name,
      adminId: row.admin_id,
      redisUrl: row.redis_url,
      b2Endpoint: row.b2_endpoint,
      b2AccessKey: row.b2_access_key,
      b2SecretKey: row.b2_secret_key,
      b2Bucket: row.b2_bucket,
      juicefsRsaKey: row.juicefs_rsa_key,
      createdAt: row.created_at,
    };
  }

  getProjectsByAdmin(adminId: string): Project[] {
    const rows = this.db
      .prepare(
        "SELECT id, name, admin_id, redis_url, b2_endpoint, b2_access_key, b2_secret_key, b2_bucket, juicefs_rsa_key, created_at FROM projects WHERE admin_id = ? ORDER BY created_at DESC"
      )
      .all(adminId) as Array<{
        id: string;
        name: string;
        admin_id: string;
        redis_url: string;
        b2_endpoint: string;
        b2_access_key: string;
        b2_secret_key: string;
        b2_bucket: string;
        juicefs_rsa_key: string;
        created_at: string;
      }>;

    return rows.map((row) => ({
      id: row.id,
      name: row.name,
      adminId: row.admin_id,
      redisUrl: row.redis_url,
      b2Endpoint: row.b2_endpoint,
      b2AccessKey: row.b2_access_key,
      b2SecretKey: row.b2_secret_key,
      b2Bucket: row.b2_bucket,
      juicefsRsaKey: row.juicefs_rsa_key,
      createdAt: row.created_at,
    }));
  }

  createProject(project: Project): void {
    this.db
      .prepare(
        "INSERT INTO projects (id, name, admin_id, redis_url, b2_endpoint, b2_access_key, b2_secret_key, b2_bucket, juicefs_rsa_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
      )
      .run(
        project.id,
        project.name,
        project.adminId,
        project.redisUrl,
        project.b2Endpoint,
        project.b2AccessKey,
        project.b2SecretKey,
        project.b2Bucket,
        project.juicefsRsaKey,
        project.createdAt
      );
  }

  // --- Artist methods ---

  getArtist(id: string): Artist | null {
    const row = this.db
      .prepare(
        "SELECT id, name, email, api_key, project_id, created_at, revoked_at FROM artists WHERE id = ?"
      )
      .get(id) as {
        id: string;
        name: string;
        email: string;
        api_key: string;
        project_id: string;
        created_at: string;
        revoked_at: string | null;
      } | undefined;

    if (!row) return null;
    return {
      id: row.id,
      name: row.name,
      email: row.email,
      apiKey: row.api_key,
      projectId: row.project_id,
      createdAt: row.created_at,
      ...(row.revoked_at ? { revokedAt: row.revoked_at } : {}),
    };
  }

  getArtistByApiKey(apiKey: string): Artist | null {
    const row = this.db
      .prepare(
        "SELECT id, name, email, api_key, project_id, created_at, revoked_at FROM artists WHERE api_key = ?"
      )
      .get(apiKey) as {
        id: string;
        name: string;
        email: string;
        api_key: string;
        project_id: string;
        created_at: string;
        revoked_at: string | null;
      } | undefined;

    if (!row) return null;
    return {
      id: row.id,
      name: row.name,
      email: row.email,
      apiKey: row.api_key,
      projectId: row.project_id,
      createdAt: row.created_at,
      ...(row.revoked_at ? { revokedAt: row.revoked_at } : {}),
    };
  }

  createArtist(artist: Artist): void {
    this.db
      .prepare(
        "INSERT INTO artists (id, name, email, api_key, project_id, created_at) VALUES (?, ?, ?, ?, ?, ?)"
      )
      .run(artist.id, artist.name, artist.email, artist.apiKey, artist.projectId, artist.createdAt);
  }

  revokeArtist(id: string): void {
    this.db
      .prepare("UPDATE artists SET revoked_at = ? WHERE id = ?")
      .run(new Date().toISOString(), id);
  }

  listArtists(projectId: string): Artist[] {
    const rows = this.db
      .prepare(
        "SELECT id, name, email, api_key, project_id, created_at, revoked_at FROM artists WHERE project_id = ? ORDER BY created_at DESC"
      )
      .all(projectId) as Array<{
        id: string;
        name: string;
        email: string;
        api_key: string;
        project_id: string;
        created_at: string;
        revoked_at: string | null;
      }>;

    return rows.map((row) => ({
      id: row.id,
      name: row.name,
      email: row.email,
      apiKey: row.api_key,
      projectId: row.project_id,
      createdAt: row.created_at,
      ...(row.revoked_at ? { revokedAt: row.revoked_at } : {}),
    }));
  }

  close(): void {
    this.db.close();
  }
}
