fn main() {
    // Build Swift bridge for File Provider domain registration
    #[cfg(target_os = "macos")]
    {
        use std::process::Command;
        let swift_bridge = "swift-bridge/FileProviderBridge.swift";
        if std::path::Path::new(swift_bridge).exists() {
            let out_dir = std::env::var("OUT_DIR").unwrap();
            let status = Command::new("swiftc")
                .args([
                    "-emit-library",
                    "-o",
                    &format!("{}/libfileprovider_bridge.dylib", out_dir),
                    "-emit-module",
                    "-module-name",
                    "FileProviderBridge",
                    "-framework",
                    "FileProvider",
                    swift_bridge,
                ])
                .status()
                .expect("Failed to compile Swift bridge");

            if status.success() {
                println!("cargo:rustc-link-search=native={}", out_dir);
                println!("cargo:rustc-link-lib=dylib=fileprovider_bridge");
            } else {
                eprintln!(
                    "Warning: Swift bridge compilation failed, File Provider features disabled"
                );
            }
        }
    }

    tauri_build::build()
}
