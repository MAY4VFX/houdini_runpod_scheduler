import FileProvider
import UniformTypeIdentifiers

/// Represents a single item (file or folder) in the File Provider domain.
class FileProviderItem: NSObject, NSFileProviderItem {

    private let record: MetadataDB.ItemRecord

    init(record: MetadataDB.ItemRecord) {
        self.record = record
        super.init()
    }

    // MARK: - Required properties

    var itemIdentifier: NSFileProviderItemIdentifier {
        if record.identifier == "/" || record.identifier.isEmpty {
            return .rootContainer
        }
        return NSFileProviderItemIdentifier(record.identifier)
    }

    var parentItemIdentifier: NSFileProviderItemIdentifier {
        let parent = record.parentIdentifier
        if parent.isEmpty || parent == "/" {
            return .rootContainer
        }
        return NSFileProviderItemIdentifier(parent)
    }

    var capabilities: NSFileProviderItemCapabilities {
        if record.isFolder {
            return [.allowsReading, .allowsContentEnumerating,
                    .allowsAddingSubItems, .allowsRenaming, .allowsDeleting]
        }
        return [.allowsReading, .allowsWriting, .allowsRenaming, .allowsDeleting]
    }

    var filename: String {
        record.filename
    }

    var contentType: UTType {
        if record.isFolder {
            return .folder
        }
        return UTType(filenameExtension: (record.filename as NSString).pathExtension) ?? .data
    }

    // MARK: - Optional but important properties

    var documentSize: NSNumber? {
        record.isFolder ? nil : NSNumber(value: record.size)
    }

    var contentModificationDate: Date? {
        record.contentModifiedAt
    }

    var creationDate: Date? {
        record.contentModifiedAt  // S3 doesn't track creation date separately
    }

    var itemVersion: NSFileProviderItemVersion {
        let etagData = record.etag.data(using: .utf8) ?? Data()
        return NSFileProviderItemVersion(contentVersion: etagData, metadataVersion: etagData)
    }

    var isDownloaded: Bool {
        record.isDownloaded
    }

    // Show cloud status in Finder
    var isUploaded: Bool {
        true  // All items in our domain come from the server
    }

    var isMostRecentVersionDownloaded: Bool {
        record.isDownloaded
    }

    // MARK: - Factory helpers

    /// Create a FileProviderItem for a root container.
    static func rootItem() -> FileProviderItem {
        FileProviderItem(record: MetadataDB.ItemRecord(
            identifier: "/",
            parentIdentifier: "",
            filename: "RunPodFarm",
            isFolder: true,
            size: 0,
            etag: "",
            contentModifiedAt: Date(),
            lastSyncedAt: Date(),
            isDownloaded: true
        ))
    }

    /// Create from an S3 object (file).
    static func fromS3Object(_ obj: S3Backend.S3Object, parentIdentifier: String) -> FileProviderItem {
        let filename = (obj.key as NSString).lastPathComponent
        let record = MetadataDB.ItemRecord(
            identifier: obj.key,
            parentIdentifier: parentIdentifier,
            filename: filename,
            isFolder: false,
            size: obj.size,
            etag: obj.etag,
            contentModifiedAt: obj.lastModified,
            lastSyncedAt: Date(),
            isDownloaded: false
        )
        return FileProviderItem(record: record)
    }

    /// Create from an S3 common prefix (folder).
    static func fromS3Prefix(_ prefix: String, parentIdentifier: String) -> FileProviderItem {
        // S3 prefixes end with "/", e.g. "projects/myproject/"
        let trimmed = prefix.hasSuffix("/") ? String(prefix.dropLast()) : prefix
        let filename = (trimmed as NSString).lastPathComponent
        let record = MetadataDB.ItemRecord(
            identifier: prefix,
            parentIdentifier: parentIdentifier,
            filename: filename,
            isFolder: true,
            size: 0,
            etag: "",
            contentModifiedAt: Date(),
            lastSyncedAt: Date(),
            isDownloaded: true
        )
        return FileProviderItem(record: record)
    }
}
