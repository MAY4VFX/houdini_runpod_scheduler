import Foundation
#if canImport(FoundationXML)
import FoundationXML
#endif

/// Minimal S3 client for communicating with JuiceFS Gateway (MinIO-compatible).
/// Uses only Foundation (no AWS SDK dependency) for lightweight extension binary.
actor S3Backend {
    private let endpoint: URL
    private let accessKey: String
    private let secretKey: String
    private let session: URLSession

    init(endpoint: URL, accessKey: String, secretKey: String) {
        self.endpoint = endpoint
        self.accessKey = accessKey
        self.secretKey = secretKey
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 300
        self.session = URLSession(configuration: config)
    }

    // MARK: - Public API

    struct S3Object {
        let key: String
        let size: Int64
        let etag: String
        let lastModified: Date
    }

    struct ListResult {
        let objects: [S3Object]
        let commonPrefixes: [String]  // "subdirectories"
        let isTruncated: Bool
        let nextContinuationToken: String?
    }

    /// List objects under a prefix (single level with delimiter).
    func listObjects(prefix: String, delimiter: String = "/",
                     continuationToken: String? = nil, maxKeys: Int = 1000) async throws -> ListResult {
        var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "list-type", value: "2"),
            URLQueryItem(name: "prefix", value: prefix),
            URLQueryItem(name: "delimiter", value: delimiter),
            URLQueryItem(name: "max-keys", value: String(maxKeys))
        ]
        if let token = continuationToken {
            components.queryItems?.append(URLQueryItem(name: "continuation-token", value: token))
        }

        var request = URLRequest(url: components.url!)
        request.httpMethod = "GET"
        signRequest(&request)

        let (data, response) = try await session.data(for: request)
        try checkResponse(response, data: data)
        return try parseListResult(data)
    }

    /// Get object metadata (HEAD).
    func headObject(key: String) async throws -> S3Object {
        let url = endpoint.appendingPathComponent(key)
        var request = URLRequest(url: url)
        request.httpMethod = "HEAD"
        signRequest(&request)

        let (_, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw S3Error.invalidResponse
        }
        guard httpResponse.statusCode == 200 else {
            throw S3Error.httpError(httpResponse.statusCode, "HEAD \(key)")
        }

        let size = Int64(httpResponse.value(forHTTPHeaderField: "Content-Length") ?? "0") ?? 0
        let etag = httpResponse.value(forHTTPHeaderField: "ETag")?.trimmingCharacters(in: CharacterSet(charactersIn: "\"")) ?? ""
        let lastModified = parseHTTPDate(httpResponse.value(forHTTPHeaderField: "Last-Modified") ?? "") ?? Date()

        return S3Object(key: key, size: size, etag: etag, lastModified: lastModified)
    }

    /// Download object to a local file.
    func getObject(key: String, destinationURL: URL) async throws {
        let url = endpoint.appendingPathComponent(key)
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        signRequest(&request)

        let (tempURL, response) = try await session.download(for: request)
        try checkResponse(response, data: nil)

        let fm = FileManager.default
        if fm.fileExists(atPath: destinationURL.path) {
            try fm.removeItem(at: destinationURL)
        }
        try fm.createDirectory(at: destinationURL.deletingLastPathComponent(),
                               withIntermediateDirectories: true)
        try fm.moveItem(at: tempURL, to: destinationURL)
    }

    /// Upload a file as an S3 object.
    func putObject(key: String, fileURL: URL) async throws -> String {
        let url = endpoint.appendingPathComponent(key)
        var request = URLRequest(url: url)
        request.httpMethod = "PUT"

        let fileData = try Data(contentsOf: fileURL)
        request.httpBody = fileData
        request.setValue("\(fileData.count)", forHTTPHeaderField: "Content-Length")
        signRequest(&request)

        let (data, response) = try await session.data(for: request)
        try checkResponse(response, data: data)

        let httpResponse = response as! HTTPURLResponse
        let etag = httpResponse.value(forHTTPHeaderField: "ETag")?.trimmingCharacters(in: CharacterSet(charactersIn: "\"")) ?? ""
        return etag
    }

    /// Delete an object.
    func deleteObject(key: String) async throws {
        let url = endpoint.appendingPathComponent(key)
        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        signRequest(&request)

        let (data, response) = try await session.data(for: request)
        try checkResponse(response, data: data)
    }

    // MARK: - Auth (S3 V4 Signing)

    private func signRequest(_ request: inout URLRequest) {
        // MinIO/JuiceFS Gateway on localhost: use basic auth header
        // S3 V4 signing is complex; for localhost gateway we use the simpler
        // Authorization header approach that MinIO accepts
        let dateFormatter = DateFormatter()
        dateFormatter.locale = Locale(identifier: "en_US_POSIX")
        dateFormatter.timeZone = TimeZone(identifier: "UTC")
        dateFormatter.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        let amzDate = dateFormatter.string(from: Date())

        dateFormatter.dateFormat = "yyyyMMdd"
        let dateStamp = dateFormatter.string(from: Date())

        request.setValue(amzDate, forHTTPHeaderField: "x-amz-date")

        let method = request.httpMethod ?? "GET"
        let url = request.url!
        let path = url.path.isEmpty ? "/" : url.path
        let query = url.query ?? ""

        let host = url.host ?? "localhost"
        let port = url.port
        let hostHeader = port != nil ? "\(host):\(port!)" : host
        request.setValue(hostHeader, forHTTPHeaderField: "Host")

        // Canonical headers (sorted)
        let signedHeaders = "host;x-amz-date"
        let canonicalHeaders = "host:\(hostHeader)\nx-amz-date:\(amzDate)\n"

        // Payload hash
        let payloadHash: String
        if let body = request.httpBody {
            payloadHash = sha256Hex(body)
        } else {
            payloadHash = sha256Hex(Data()) // empty body
        }
        request.setValue(payloadHash, forHTTPHeaderField: "x-amz-content-sha256")

        // Canonical request
        let canonicalQueryString = canonicalizeQueryString(query)
        let canonicalRequest = [method, path, canonicalQueryString, canonicalHeaders, signedHeaders, payloadHash].joined(separator: "\n")

        let region = "us-east-1"
        let service = "s3"
        let scope = "\(dateStamp)/\(region)/\(service)/aws4_request"

        let stringToSign = "AWS4-HMAC-SHA256\n\(amzDate)\n\(scope)\n\(sha256Hex(canonicalRequest.data(using: .utf8)!))"

        // Signing key
        let kDate = hmacSHA256(key: "AWS4\(secretKey)".data(using: .utf8)!, data: dateStamp.data(using: .utf8)!)
        let kRegion = hmacSHA256(key: kDate, data: region.data(using: .utf8)!)
        let kService = hmacSHA256(key: kRegion, data: service.data(using: .utf8)!)
        let kSigning = hmacSHA256(key: kService, data: "aws4_request".data(using: .utf8)!)

        let signature = hmacSHA256(key: kSigning, data: stringToSign.data(using: .utf8)!).map { String(format: "%02x", $0) }.joined()

        let authHeader = "AWS4-HMAC-SHA256 Credential=\(accessKey)/\(scope), SignedHeaders=\(signedHeaders), Signature=\(signature)"
        request.setValue(authHeader, forHTTPHeaderField: "Authorization")
    }

    private func canonicalizeQueryString(_ query: String) -> String {
        guard !query.isEmpty else { return "" }
        let pairs = query.split(separator: "&").map { pair -> (String, String) in
            let parts = pair.split(separator: "=", maxSplits: 1)
            let key = String(parts[0])
            let value = parts.count > 1 ? String(parts[1]) : ""
            return (key, value)
        }
        return pairs.sorted { $0.0 < $1.0 }.map { "\($0.0)=\($0.1)" }.joined(separator: "&")
    }

    // MARK: - Crypto helpers (CommonCrypto)

    private func sha256Hex(_ data: Data) -> String {
        var hash = [UInt8](repeating: 0, count: 32)
        data.withUnsafeBytes { buffer in
            _ = CC_SHA256(buffer.baseAddress, CC_LONG(data.count), &hash)
        }
        return hash.map { String(format: "%02x", $0) }.joined()
    }

    private func hmacSHA256(key: Data, data: Data) -> Data {
        var result = [UInt8](repeating: 0, count: 32)
        key.withUnsafeBytes { keyBuffer in
            data.withUnsafeBytes { dataBuffer in
                CCHmac(CCHmacAlgorithm(kCCHmacAlgSHA256),
                        keyBuffer.baseAddress, key.count,
                        dataBuffer.baseAddress, data.count,
                        &result)
            }
        }
        return Data(result)
    }

    // MARK: - XML parsing

    private func parseListResult(_ data: Data) throws -> ListResult {
        let parser = S3ListResultParser(data: data)
        return try parser.parse()
    }

    // MARK: - Helpers

    private func checkResponse(_ response: URLResponse, data: Data?) throws {
        guard let httpResponse = response as? HTTPURLResponse else {
            throw S3Error.invalidResponse
        }
        guard (200...299).contains(httpResponse.statusCode) else {
            let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
            throw S3Error.httpError(httpResponse.statusCode, body)
        }
    }

    private func parseHTTPDate(_ string: String) -> Date? {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        // RFC 7231: "Sun, 06 Nov 1994 08:49:37 GMT"
        formatter.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
        return formatter.date(from: string)
    }

    // MARK: - Errors

    enum S3Error: Error, LocalizedError {
        case invalidResponse
        case httpError(Int, String)
        case parseError(String)

        var errorDescription: String? {
            switch self {
            case .invalidResponse: return "Invalid response from S3 gateway"
            case .httpError(let code, let msg): return "S3 error \(code): \(msg)"
            case .parseError(let msg): return "S3 parse error: \(msg)"
            }
        }
    }
}

// MARK: - CommonCrypto import

import CommonCrypto

// MARK: - Simple XML parser for ListObjectsV2 response

private class S3ListResultParser: NSObject, XMLParserDelegate {
    private let data: Data
    private var objects: [S3Backend.S3Object] = []
    private var commonPrefixes: [String] = []
    private var isTruncated = false
    private var nextContinuationToken: String?

    private var currentElement = ""
    private var currentText = ""

    // Current <Contents> fields
    private var currentKey = ""
    private var currentSize: Int64 = 0
    private var currentETag = ""
    private var currentLastModified = Date()
    private var inContents = false
    private var inCommonPrefixes = false

    init(data: Data) {
        self.data = data
    }

    func parse() throws -> S3Backend.ListResult {
        let parser = XMLParser(data: data)
        parser.delegate = self
        guard parser.parse() else {
            throw S3Backend.S3Error.parseError("Failed to parse ListObjectsV2 XML")
        }
        return S3Backend.ListResult(objects: objects, commonPrefixes: commonPrefixes,
                                     isTruncated: isTruncated,
                                     nextContinuationToken: nextContinuationToken)
    }

    func parser(_ parser: XMLParser, didStartElement elementName: String,
                namespaceURI: String?, qualifiedName qName: String?,
                attributes attributeDict: [String: String] = [:]) {
        currentElement = elementName
        currentText = ""
        if elementName == "Contents" { inContents = true }
        if elementName == "CommonPrefixes" { inCommonPrefixes = true }
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) {
        currentText += string
    }

    func parser(_ parser: XMLParser, didEndElement elementName: String,
                namespaceURI: String?, qualifiedName qName: String?) {
        let text = currentText.trimmingCharacters(in: .whitespacesAndNewlines)

        if inContents {
            switch elementName {
            case "Key": currentKey = text
            case "Size": currentSize = Int64(text) ?? 0
            case "ETag": currentETag = text.trimmingCharacters(in: CharacterSet(charactersIn: "\""))
            case "LastModified": currentLastModified = parseISO8601(text) ?? Date()
            case "Contents":
                objects.append(S3Backend.S3Object(key: currentKey, size: currentSize,
                                                   etag: currentETag, lastModified: currentLastModified))
                inContents = false
                currentKey = ""
                currentSize = 0
                currentETag = ""
            default: break
            }
        } else if inCommonPrefixes {
            switch elementName {
            case "Prefix": commonPrefixes.append(text)
            case "CommonPrefixes": inCommonPrefixes = false
            default: break
            }
        } else {
            switch elementName {
            case "IsTruncated": isTruncated = text.lowercased() == "true"
            case "NextContinuationToken": nextContinuationToken = text
            default: break
            }
        }
    }

    private func parseISO8601(_ string: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.date(from: string) ?? ISO8601DateFormatter().date(from: string)
    }
}
