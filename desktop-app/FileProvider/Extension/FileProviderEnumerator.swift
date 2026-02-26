import FileProvider

/// Enumerates files and folders from JuiceFS Gateway via S3 API.
class FileProviderEnumerator: NSObject, NSFileProviderEnumerator {

    private let enumeratedItemIdentifier: NSFileProviderItemIdentifier
    private let s3: S3Backend
    private let db: MetadataDB

    init(enumeratedItemIdentifier: NSFileProviderItemIdentifier, s3: S3Backend, db: MetadataDB) {
        self.enumeratedItemIdentifier = enumeratedItemIdentifier
        self.s3 = s3
        self.db = db
        super.init()
    }

    func invalidate() {
        // No-op: no long-running state to clean up
    }

    // MARK: - Enumerate items

    func enumerateItems(for observer: NSFileProviderEnumerationObserver, startingAt page: NSFileProviderPage) {
        Task {
            do {
                let prefix = s3PrefixForIdentifier(enumeratedItemIdentifier)
                let continuationToken: String? = (page != NSFileProviderPage.initialPageSortedByName as NSFileProviderPage)
                    ? String(data: page.rawValue, encoding: .utf8) : nil

                let result = try await s3.listObjects(prefix: prefix, continuationToken: continuationToken)

                var items: [NSFileProviderItem] = []
                let parentId = identifierString(enumeratedItemIdentifier)

                // Folders (common prefixes)
                for prefixStr in result.commonPrefixes {
                    let item = FileProviderItem.fromS3Prefix(prefixStr, parentIdentifier: parentId)
                    items.append(item)
                    try? db.upsertItem(itemRecordFromS3Prefix(prefixStr, parent: parentId))
                }

                // Files
                for obj in result.objects {
                    // Skip the prefix itself (S3 returns the folder as an object sometimes)
                    if obj.key == prefix { continue }
                    // Skip "directory marker" objects (size 0, key ends with /)
                    if obj.key.hasSuffix("/") && obj.size == 0 { continue }

                    let item = FileProviderItem.fromS3Object(obj, parentIdentifier: parentId)
                    items.append(item)
                    try? db.upsertItem(itemRecordFromS3Object(obj, parent: parentId))
                }

                observer.didEnumerate(items)

                if result.isTruncated, let token = result.nextContinuationToken {
                    let nextPage = NSFileProviderPage(token.data(using: .utf8)!)
                    observer.finishEnumerating(upTo: nextPage)
                } else {
                    // Update sync anchor
                    let anchor = ISO8601DateFormatter().string(from: Date())
                    try? db.setSyncAnchor(anchor)
                    observer.finishEnumerating(upTo: nil)
                }
            } catch {
                NSLog("RunPodFarm: Enumeration failed for \(enumeratedItemIdentifier.rawValue): \(error)")
                observer.finishEnumeratingWithError(error)
            }
        }
    }

    // MARK: - Enumerate changes (for sync)

    func enumerateChanges(for observer: NSFileProviderChangeObserver, from anchor: NSFileProviderSyncAnchor) {
        Task {
            do {
                let prefix = s3PrefixForIdentifier(enumeratedItemIdentifier)
                let parentId = identifierString(enumeratedItemIdentifier)

                // Get current items from S3
                var allObjects: [S3Backend.S3Object] = []
                var allPrefixes: [String] = []
                var continuationToken: String? = nil

                repeat {
                    let result = try await s3.listObjects(prefix: prefix, continuationToken: continuationToken)
                    allObjects.append(contentsOf: result.objects)
                    allPrefixes.append(contentsOf: result.commonPrefixes)
                    continuationToken = result.isTruncated ? result.nextContinuationToken : nil
                } while continuationToken != nil

                // Get previously known items from DB
                let knownItems = (try? db.getChildren(parentIdentifier: parentId)) ?? []
                let knownIds = Set(knownItems.map { $0.identifier })

                var updatedItems: [NSFileProviderItem] = []
                var deletedIds: [NSFileProviderItemIdentifier] = []
                var currentIds = Set<String>()

                // Check folders
                for prefixStr in allPrefixes {
                    currentIds.insert(prefixStr)
                    let item = FileProviderItem.fromS3Prefix(prefixStr, parentIdentifier: parentId)
                    updatedItems.append(item)
                    try? db.upsertItem(itemRecordFromS3Prefix(prefixStr, parent: parentId))
                }

                // Check files
                for obj in allObjects {
                    if obj.key == prefix { continue }
                    if obj.key.hasSuffix("/") && obj.size == 0 { continue }
                    currentIds.insert(obj.key)

                    // Check if etag changed
                    if let known = knownItems.first(where: { $0.identifier == obj.key }) {
                        if known.etag != obj.etag {
                            let item = FileProviderItem.fromS3Object(obj, parentIdentifier: parentId)
                            updatedItems.append(item)
                            try? db.upsertItem(itemRecordFromS3Object(obj, parent: parentId))
                        }
                    } else {
                        // New file
                        let item = FileProviderItem.fromS3Object(obj, parentIdentifier: parentId)
                        updatedItems.append(item)
                        try? db.upsertItem(itemRecordFromS3Object(obj, parent: parentId))
                    }
                }

                // Detect deletions
                for knownId in knownIds {
                    if !currentIds.contains(knownId) {
                        deletedIds.append(NSFileProviderItemIdentifier(knownId))
                        try? db.deleteItem(identifier: knownId)
                    }
                }

                if !updatedItems.isEmpty {
                    observer.didUpdate(updatedItems)
                }
                if !deletedIds.isEmpty {
                    observer.didDeleteItems(withIdentifiers: deletedIds)
                }

                let newAnchor = ISO8601DateFormatter().string(from: Date())
                try? db.setSyncAnchor(newAnchor)
                let anchorData = newAnchor.data(using: .utf8) ?? Data()
                observer.finishEnumeratingChanges(upTo: NSFileProviderSyncAnchor(anchorData), moreComing: false)
            } catch {
                NSLog("RunPodFarm: Change enumeration failed: \(error)")
                observer.finishEnumeratingWithError(error)
            }
        }
    }

    func currentSyncAnchor(completionHandler: @escaping (NSFileProviderSyncAnchor?) -> Void) {
        if let anchor = db.getSyncAnchor(),
           let data = anchor.data(using: .utf8) {
            completionHandler(NSFileProviderSyncAnchor(data))
        } else {
            completionHandler(nil)
        }
    }

    // MARK: - Helpers

    private func s3PrefixForIdentifier(_ identifier: NSFileProviderItemIdentifier) -> String {
        switch identifier {
        case .rootContainer:
            return ""  // List everything at root
        case .workingSet:
            return ""  // Working set = root for now
        default:
            let raw = identifier.rawValue
            // Ensure prefix ends with /
            return raw.hasSuffix("/") ? raw : raw + "/"
        }
    }

    private func identifierString(_ identifier: NSFileProviderItemIdentifier) -> String {
        switch identifier {
        case .rootContainer: return "/"
        case .workingSet: return "/"
        default: return identifier.rawValue
        }
    }

    private func itemRecordFromS3Object(_ obj: S3Backend.S3Object, parent: String) -> MetadataDB.ItemRecord {
        MetadataDB.ItemRecord(
            identifier: obj.key,
            parentIdentifier: parent,
            filename: (obj.key as NSString).lastPathComponent,
            isFolder: false,
            size: obj.size,
            etag: obj.etag,
            contentModifiedAt: obj.lastModified,
            lastSyncedAt: Date(),
            isDownloaded: false
        )
    }

    private func itemRecordFromS3Prefix(_ prefix: String, parent: String) -> MetadataDB.ItemRecord {
        let trimmed = prefix.hasSuffix("/") ? String(prefix.dropLast()) : prefix
        return MetadataDB.ItemRecord(
            identifier: prefix,
            parentIdentifier: parent,
            filename: (trimmed as NSString).lastPathComponent,
            isFolder: true,
            size: 0,
            etag: "",
            contentModifiedAt: Date(),
            lastSyncedAt: Date(),
            isDownloaded: true
        )
    }
}
