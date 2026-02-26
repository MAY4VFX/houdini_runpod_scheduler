import Foundation

/// Reads configuration from App Groups shared container.
/// Written by Tauri app, read by File Provider Extension.
struct SharedConfig {
    static let appGroupIdentifier = "group.com.runpodfarm.desktop"

    struct GatewayConfig: Codable {
        let gatewayUrl: String
        let gatewayAccessKey: String
        let gatewaySecretKey: String
        let projectId: String

        enum CodingKeys: String, CodingKey {
            case gatewayUrl = "gateway_url"
            case gatewayAccessKey = "gateway_access_key"
            case gatewaySecretKey = "gateway_secret_key"
            case projectId = "project_id"
        }
    }

    static var containerURL: URL? {
        FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: appGroupIdentifier)
    }

    static var configFileURL: URL? {
        containerURL?.appendingPathComponent("config.json")
    }

    static func load() -> GatewayConfig? {
        guard let url = configFileURL,
              FileManager.default.fileExists(atPath: url.path) else {
            return nil
        }
        do {
            let data = try Data(contentsOf: url)
            return try JSONDecoder().decode(GatewayConfig.self, from: data)
        } catch {
            NSLog("RunPodFarm: Failed to read shared config: \(error)")
            return nil
        }
    }

    static func save(_ config: GatewayConfig) throws {
        guard let url = configFileURL else {
            throw NSError(domain: "SharedConfig", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "App Group container not available"])
        }
        let dir = url.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let data = try JSONEncoder().encode(config)
        try data.write(to: url, options: .atomic)
    }
}
