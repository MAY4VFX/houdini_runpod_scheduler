import Foundation
import SQLite3

/// SQLite-backed metadata cache for File Provider items.
/// Stores file metadata and sync state to avoid redundant S3 calls.
final class MetadataDB {
    private var db: OpaquePointer?
    private let queue = DispatchQueue(label: "com.runpodfarm.metadatadb", qos: .userInitiated)

    struct ItemRecord {
        let identifier: String      // S3 key (path)
        let parentIdentifier: String
        let filename: String
        let isFolder: Bool
        let size: Int64
        let etag: String
        let contentModifiedAt: Date
        let lastSyncedAt: Date
        let isDownloaded: Bool
    }

    init(at url: URL) throws {
        let dir = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        var dbPointer: OpaquePointer?
        let rc = sqlite3_open_v2(url.path, &dbPointer,
                                  SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nil)
        guard rc == SQLITE_OK, let db = dbPointer else {
            let msg = dbPointer.flatMap { String(cString: sqlite3_errmsg($0)) } ?? "Unknown error"
            sqlite3_close(dbPointer)
            throw DBError.openFailed(msg)
        }
        self.db = db

        // WAL mode for concurrent reads
        sqlite3_exec(db, "PRAGMA journal_mode=WAL", nil, nil, nil)
        sqlite3_exec(db, "PRAGMA synchronous=NORMAL", nil, nil, nil)

        try createTables()
    }

    deinit {
        if let db = db {
            sqlite3_close(db)
        }
    }

    // MARK: - Schema

    private func createTables() throws {
        let sql = """
        CREATE TABLE IF NOT EXISTS items (
            identifier TEXT PRIMARY KEY,
            parent_identifier TEXT NOT NULL,
            filename TEXT NOT NULL,
            is_folder INTEGER DEFAULT 0,
            size INTEGER DEFAULT 0,
            etag TEXT DEFAULT '',
            content_modified_at REAL DEFAULT 0,
            last_synced_at REAL DEFAULT 0,
            is_downloaded INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_parent ON items(parent_identifier);

        CREATE TABLE IF NOT EXISTS sync_anchors (
            domain TEXT PRIMARY KEY,
            anchor TEXT NOT NULL
        );
        """
        var errMsg: UnsafeMutablePointer<CChar>?
        let rc = sqlite3_exec(db, sql, nil, nil, &errMsg)
        if rc != SQLITE_OK {
            let msg = errMsg.map { String(cString: $0) } ?? "Unknown"
            sqlite3_free(errMsg)
            throw DBError.queryFailed(msg)
        }
    }

    // MARK: - CRUD

    func upsertItem(_ item: ItemRecord) throws {
        try queue.sync {
            let sql = """
            INSERT INTO items (identifier, parent_identifier, filename, is_folder, size, etag,
                               content_modified_at, last_synced_at, is_downloaded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identifier) DO UPDATE SET
                parent_identifier = excluded.parent_identifier,
                filename = excluded.filename,
                is_folder = excluded.is_folder,
                size = excluded.size,
                etag = excluded.etag,
                content_modified_at = excluded.content_modified_at,
                last_synced_at = excluded.last_synced_at,
                is_downloaded = CASE WHEN excluded.etag != items.etag THEN 0 ELSE items.is_downloaded END
            """
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
                throw DBError.queryFailed(lastError)
            }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (item.identifier as NSString).utf8String, -1, nil)
            sqlite3_bind_text(stmt, 2, (item.parentIdentifier as NSString).utf8String, -1, nil)
            sqlite3_bind_text(stmt, 3, (item.filename as NSString).utf8String, -1, nil)
            sqlite3_bind_int(stmt, 4, item.isFolder ? 1 : 0)
            sqlite3_bind_int64(stmt, 5, item.size)
            sqlite3_bind_text(stmt, 6, (item.etag as NSString).utf8String, -1, nil)
            sqlite3_bind_double(stmt, 7, item.contentModifiedAt.timeIntervalSince1970)
            sqlite3_bind_double(stmt, 8, item.lastSyncedAt.timeIntervalSince1970)
            sqlite3_bind_int(stmt, 9, item.isDownloaded ? 1 : 0)

            guard sqlite3_step(stmt) == SQLITE_DONE else {
                throw DBError.queryFailed(lastError)
            }
        }
    }

    func getItem(identifier: String) throws -> ItemRecord? {
        try queue.sync {
            let sql = "SELECT * FROM items WHERE identifier = ?"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
                throw DBError.queryFailed(lastError)
            }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (identifier as NSString).utf8String, -1, nil)

            guard sqlite3_step(stmt) == SQLITE_ROW else { return nil }
            return readRow(stmt)
        }
    }

    func getChildren(parentIdentifier: String) throws -> [ItemRecord] {
        try queue.sync {
            let sql = "SELECT * FROM items WHERE parent_identifier = ? ORDER BY is_folder DESC, filename ASC"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
                throw DBError.queryFailed(lastError)
            }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (parentIdentifier as NSString).utf8String, -1, nil)

            var results: [ItemRecord] = []
            while sqlite3_step(stmt) == SQLITE_ROW {
                results.append(readRow(stmt))
            }
            return results
        }
    }

    func markDownloaded(identifier: String, downloaded: Bool) throws {
        try queue.sync {
            let sql = "UPDATE items SET is_downloaded = ? WHERE identifier = ?"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
                throw DBError.queryFailed(lastError)
            }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_int(stmt, 1, downloaded ? 1 : 0)
            sqlite3_bind_text(stmt, 2, (identifier as NSString).utf8String, -1, nil)
            sqlite3_step(stmt)
        }
    }

    func deleteItem(identifier: String) throws {
        try queue.sync {
            let sql = "DELETE FROM items WHERE identifier = ?"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
                throw DBError.queryFailed(lastError)
            }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (identifier as NSString).utf8String, -1, nil)
            sqlite3_step(stmt)
        }
    }

    func deleteChildren(parentIdentifier: String) throws {
        try queue.sync {
            let sql = "DELETE FROM items WHERE parent_identifier = ?"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
                throw DBError.queryFailed(lastError)
            }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (parentIdentifier as NSString).utf8String, -1, nil)
            sqlite3_step(stmt)
        }
    }

    // MARK: - Sync anchors

    func getSyncAnchor(domain: String = "default") -> String? {
        queue.sync {
            let sql = "SELECT anchor FROM sync_anchors WHERE domain = ?"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (domain as NSString).utf8String, -1, nil)
            guard sqlite3_step(stmt) == SQLITE_ROW else { return nil }
            return String(cString: sqlite3_column_text(stmt, 0))
        }
    }

    func setSyncAnchor(_ anchor: String, domain: String = "default") throws {
        try queue.sync {
            let sql = "INSERT OR REPLACE INTO sync_anchors (domain, anchor) VALUES (?, ?)"
            var stmt: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else {
                throw DBError.queryFailed(lastError)
            }
            defer { sqlite3_finalize(stmt) }

            sqlite3_bind_text(stmt, 1, (domain as NSString).utf8String, -1, nil)
            sqlite3_bind_text(stmt, 2, (anchor as NSString).utf8String, -1, nil)
            sqlite3_step(stmt)
        }
    }

    // MARK: - Helpers

    private func readRow(_ stmt: OpaquePointer?) -> ItemRecord {
        ItemRecord(
            identifier: String(cString: sqlite3_column_text(stmt, 0)),
            parentIdentifier: String(cString: sqlite3_column_text(stmt, 1)),
            filename: String(cString: sqlite3_column_text(stmt, 2)),
            isFolder: sqlite3_column_int(stmt, 3) != 0,
            size: sqlite3_column_int64(stmt, 4),
            etag: String(cString: sqlite3_column_text(stmt, 5)),
            contentModifiedAt: Date(timeIntervalSince1970: sqlite3_column_double(stmt, 6)),
            lastSyncedAt: Date(timeIntervalSince1970: sqlite3_column_double(stmt, 7)),
            isDownloaded: sqlite3_column_int(stmt, 8) != 0
        )
    }

    private var lastError: String {
        db.flatMap { String(cString: sqlite3_errmsg($0)) } ?? "Unknown error"
    }

    enum DBError: Error, LocalizedError {
        case openFailed(String)
        case queryFailed(String)

        var errorDescription: String? {
            switch self {
            case .openFailed(let msg): return "DB open failed: \(msg)"
            case .queryFailed(let msg): return "DB query failed: \(msg)"
            }
        }
    }
}
