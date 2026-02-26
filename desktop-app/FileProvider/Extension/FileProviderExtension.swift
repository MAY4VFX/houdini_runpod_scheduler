import FileProvider
import os.log

/// Main File Provider extension class implementing NSFileProviderReplicatedExtension.
/// Provides on-demand access to JuiceFS files through S3 Gateway.
class FileProviderExtension: NSObject, NSFileProviderReplicatedExtension {

    private let domain: NSFileProviderDomain
    private var s3: S3Backend?
    private var db: MetadataDB?
    private let logger = Logger(subsystem: "com.runpodfarm.fileprovider", category: "Extension")

    required init(domain: NSFileProviderDomain) {
        self.domain = domain
        super.init()
        setupBackends()
    }

    private func setupBackends() {
        // Load config from App Groups shared container
        guard let config = SharedConfig.load() else {
            logger.error("No shared config available — extension not functional until Tauri app connects")
            return
        }

        guard let endpoint = URL(string: config.gatewayUrl) else {
            logger.error("Invalid gateway URL: \(config.gatewayUrl)")
            return
        }

        s3 = S3Backend(endpoint: endpoint, accessKey: config.gatewayAccessKey,
                        secretKey: config.gatewaySecretKey)

        // DB in the shared container so both app and extension can access
        if let containerURL = SharedConfig.containerURL {
            let dbURL = containerURL.appendingPathComponent("metadata.db")
            do {
                db = try MetadataDB(at: dbURL)
                logger.info("MetadataDB opened at \(dbURL.path)")
            } catch {
                logger.error("Failed to open MetadataDB: \(error.localizedDescription)")
            }
        }
    }

    // MARK: - Invalidation

    func invalidate() {
        // Clean up resources when the extension is being torn down
        s3 = nil
        db = nil
    }

    // MARK: - Item lookup

    func item(for identifier: NSFileProviderItemIdentifier,
              request: NSFileProviderRequest,
              completionHandler: @escaping (NSFileProviderItem?, Error?) -> Void) -> Progress {
        let progress = Progress(totalUnitCount: 1)

        if identifier == .rootContainer {
            completionHandler(FileProviderItem.rootItem(), nil)
            progress.completedUnitCount = 1
            return progress
        }

        // Try DB first (fast path)
        if let db = db, let record = try? db.getItem(identifier: identifier.rawValue) {
            completionHandler(FileProviderItem(record: record), nil)
            progress.completedUnitCount = 1
            return progress
        }

        // Fall back to S3 HEAD
        guard let s3 = s3 else {
            completionHandler(nil, NSFileProviderError(.serverUnreachable))
            return progress
        }

        Task {
            do {
                let key = identifier.rawValue
                if key.hasSuffix("/") {
                    // It's a folder — create from prefix
                    let parentPath = parentPathForKey(key)
                    let item = FileProviderItem.fromS3Prefix(key, parentIdentifier: parentPath)
                    completionHandler(item, nil)
                } else {
                    let obj = try await s3.headObject(key: key)
                    let parentPath = parentPathForKey(key)
                    let item = FileProviderItem.fromS3Object(obj, parentIdentifier: parentPath)
                    completionHandler(item, nil)
                }
                progress.completedUnitCount = 1
            } catch {
                logger.error("item(for:) failed for \(identifier.rawValue): \(error.localizedDescription)")
                completionHandler(nil, error)
            }
        }

        return progress
    }

    // MARK: - Enumeration

    func enumerator(for containerItemIdentifier: NSFileProviderItemIdentifier,
                    request: NSFileProviderRequest) throws -> NSFileProviderEnumerator {
        guard let s3 = s3, let db = db else {
            throw NSFileProviderError(.serverUnreachable)
        }
        return FileProviderEnumerator(enumeratedItemIdentifier: containerItemIdentifier, s3: s3, db: db)
    }

    // MARK: - Materialization (download on demand)

    func fetchContents(for itemIdentifier: NSFileProviderItemIdentifier,
                       version requestedVersion: NSFileProviderItemVersion?,
                       request: NSFileProviderRequest,
                       completionHandler: @escaping (URL?, NSFileProviderItem?, Error?) -> Void) -> Progress {
        let progress = Progress(totalUnitCount: 100)

        guard let s3 = s3 else {
            completionHandler(nil, nil, NSFileProviderError(.serverUnreachable))
            return progress
        }

        let key = itemIdentifier.rawValue

        Task {
            do {
                // Download to a temp file
                let tempDir = FileManager.default.temporaryDirectory
                let tempFile = tempDir.appendingPathComponent(UUID().uuidString)
                    .appendingPathExtension((key as NSString).pathExtension)

                progress.completedUnitCount = 10
                try await s3.getObject(key: key, destinationURL: tempFile)
                progress.completedUnitCount = 90

                // Mark as downloaded in DB
                try? db?.markDownloaded(identifier: key, downloaded: true)

                // Get updated item
                let obj = try await s3.headObject(key: key)
                let parentPath = parentPathForKey(key)
                let item = FileProviderItem.fromS3Object(obj, parentIdentifier: parentPath)

                progress.completedUnitCount = 100
                completionHandler(tempFile, item, nil)
            } catch {
                logger.error("fetchContents failed for \(key): \(error.localizedDescription)")
                completionHandler(nil, nil, error)
            }
        }

        return progress
    }

    // MARK: - Write operations (Phase 2 — stubs returning unsupported)

    func createItem(basedOn itemTemplate: NSFileProviderItem,
                    fields: NSFileProviderItemFields,
                    contents url: URL?,
                    options: NSFileProviderCreateItemOptions = [],
                    request: NSFileProviderRequest,
                    completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void) -> Progress {
        let progress = Progress(totalUnitCount: 100)

        guard let s3 = s3 else {
            completionHandler(nil, [], false, NSFileProviderError(.serverUnreachable))
            return progress
        }

        Task {
            do {
                let parentId = itemTemplate.parentItemIdentifier
                let parentPrefix = parentId == .rootContainer ? "" : parentId.rawValue
                let baseName = itemTemplate.filename
                let key: String

                if itemTemplate.contentType == .folder {
                    // Folder: create a zero-byte marker object with trailing /
                    key = parentPrefix + baseName + "/"
                    let tempFile = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
                    FileManager.default.createFile(atPath: tempFile.path, contents: Data())
                    _ = try await s3.putObject(key: key, fileURL: tempFile)
                    try? FileManager.default.removeItem(at: tempFile)

                    let record = MetadataDB.ItemRecord(
                        identifier: key, parentIdentifier: parentPrefix.isEmpty ? "/" : parentPrefix,
                        filename: baseName, isFolder: true, size: 0, etag: "",
                        contentModifiedAt: Date(), lastSyncedAt: Date(), isDownloaded: true
                    )
                    try? db?.upsertItem(record)
                    let item = FileProviderItem(record: record)
                    completionHandler(item, [], false, nil)
                } else {
                    // File: upload contents
                    key = parentPrefix + baseName
                    guard let fileURL = url else {
                        completionHandler(nil, [], false, NSFileProviderError(.noSuchItem))
                        return
                    }

                    let etag = try await s3.putObject(key: key, fileURL: fileURL)
                    let attrs = try FileManager.default.attributesOfItem(atPath: fileURL.path)
                    let size = (attrs[.size] as? Int64) ?? 0

                    let record = MetadataDB.ItemRecord(
                        identifier: key, parentIdentifier: parentPrefix.isEmpty ? "/" : parentPrefix,
                        filename: baseName, isFolder: false, size: size, etag: etag,
                        contentModifiedAt: Date(), lastSyncedAt: Date(), isDownloaded: true
                    )
                    try? db?.upsertItem(record)
                    let item = FileProviderItem(record: record)
                    completionHandler(item, [], false, nil)
                }
                progress.completedUnitCount = 100
            } catch {
                logger.error("createItem failed: \(error.localizedDescription)")
                completionHandler(nil, [], false, error)
            }
        }

        return progress
    }

    func modifyItem(_ item: NSFileProviderItem,
                    baseVersion version: NSFileProviderItemVersion,
                    changedFields: NSFileProviderItemFields,
                    contents newContents: URL?,
                    options: NSFileProviderModifyItemOptions = [],
                    request: NSFileProviderRequest,
                    completionHandler: @escaping (NSFileProviderItem?, NSFileProviderItemFields, Bool, Error?) -> Void) -> Progress {
        let progress = Progress(totalUnitCount: 100)

        guard let s3 = s3 else {
            completionHandler(nil, [], false, NSFileProviderError(.serverUnreachable))
            return progress
        }

        Task {
            do {
                let key = item.itemIdentifier.rawValue

                if changedFields.contains(.contents), let fileURL = newContents {
                    // Content changed — upload new version
                    // Check for conflicts: compare version with server
                    let serverObj = try? await s3.headObject(key: key)
                    let serverEtag = serverObj?.etag ?? ""
                    let localEtag = String(data: version.contentVersion, encoding: .utf8) ?? ""

                    if !serverEtag.isEmpty && !localEtag.isEmpty && serverEtag != localEtag {
                        // Conflict: server has a different version
                        logger.warning("Conflict detected for \(key): local=\(localEtag) server=\(serverEtag)")
                        // Create conflict copy
                        let ext = (key as NSString).pathExtension
                        let base = (key as NSString).deletingPathExtension
                        let conflictKey = "\(base) (conflict \(ISO8601DateFormatter().string(from: Date()))).\(ext)"
                        _ = try await s3.putObject(key: conflictKey, fileURL: fileURL)
                    }

                    let etag = try await s3.putObject(key: key, fileURL: fileURL)
                    let attrs = try FileManager.default.attributesOfItem(atPath: fileURL.path)
                    let size = (attrs[.size] as? Int64) ?? 0

                    let record = MetadataDB.ItemRecord(
                        identifier: key,
                        parentIdentifier: item.parentItemIdentifier == .rootContainer ? "/" : item.parentItemIdentifier.rawValue,
                        filename: item.filename, isFolder: false, size: size, etag: etag,
                        contentModifiedAt: Date(), lastSyncedAt: Date(), isDownloaded: true
                    )
                    try? db?.upsertItem(record)
                    let updatedItem = FileProviderItem(record: record)
                    completionHandler(updatedItem, [], false, nil)
                } else {
                    // Metadata-only change (rename, move) — not supported in Phase 1
                    completionHandler(item, [], false, nil)
                }
                progress.completedUnitCount = 100
            } catch {
                logger.error("modifyItem failed for \(item.itemIdentifier.rawValue): \(error.localizedDescription)")
                completionHandler(nil, [], false, error)
            }
        }

        return progress
    }

    func deleteItem(identifier: NSFileProviderItemIdentifier,
                    baseVersion version: NSFileProviderItemVersion,
                    options: NSFileProviderDeleteItemOptions = [],
                    request: NSFileProviderRequest,
                    completionHandler: @escaping (Error?) -> Void) -> Progress {
        let progress = Progress(totalUnitCount: 100)

        guard let s3 = s3 else {
            completionHandler(NSFileProviderError(.serverUnreachable))
            return progress
        }

        Task {
            do {
                let key = identifier.rawValue
                try await s3.deleteObject(key: key)
                try? db?.deleteItem(identifier: key)
                // If folder, also delete children from DB
                if key.hasSuffix("/") {
                    try? db?.deleteChildren(parentIdentifier: key)
                }
                progress.completedUnitCount = 100
                completionHandler(nil)
            } catch {
                logger.error("deleteItem failed for \(identifier.rawValue): \(error.localizedDescription)")
                completionHandler(error)
            }
        }

        return progress
    }

    // MARK: - Helpers

    private func parentPathForKey(_ key: String) -> String {
        let components = key.split(separator: "/", omittingEmptySubsequences: true)
        if components.count <= 1 {
            return "/"
        }
        return components.dropLast().joined(separator: "/") + "/"
    }
}
