import Foundation
import FileProvider

@_cdecl("register_file_provider_domain")
public func registerFileProviderDomain(_ identifier: UnsafePointer<CChar>, _ displayName: UnsafePointer<CChar>) -> Bool {
    let id = String(cString: identifier)
    let name = String(cString: displayName)

    let domain = NSFileProviderDomain(identifier: NSFileProviderDomainIdentifier(rawValue: id), displayName: name)

    let semaphore = DispatchSemaphore(value: 0)
    var success = false

    NSFileProviderManager.add(domain) { error in
        if let error = error {
            NSLog("RunPodFarm: Failed to add domain: \(error)")
            success = false
        } else {
            NSLog("RunPodFarm: Domain '\(name)' registered successfully")
            success = true
        }
        semaphore.signal()
    }

    semaphore.wait()
    return success
}

@_cdecl("remove_file_provider_domain")
public func removeFileProviderDomain(_ identifier: UnsafePointer<CChar>) -> Bool {
    let id = String(cString: identifier)
    let domain = NSFileProviderDomain(identifier: NSFileProviderDomainIdentifier(rawValue: id), displayName: "")

    let semaphore = DispatchSemaphore(value: 0)
    var success = false

    NSFileProviderManager.remove(domain) { error in
        if let error = error {
            NSLog("RunPodFarm: Failed to remove domain: \(error)")
            success = false
        } else {
            NSLog("RunPodFarm: Domain '\(id)' removed successfully")
            success = true
        }
        semaphore.signal()
    }

    semaphore.wait()
    return success
}
