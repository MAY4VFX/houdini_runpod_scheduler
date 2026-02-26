fn main() {
    // Build Swift bridge for File Provider domain registration
    #[cfg(target_os = "macos")]
    {
        use std::process::Command;
        let swift_bridge = "swift-bridge/FileProviderBridge.swift";
        if std::path::Path::new(swift_bridge).exists() {
            let out_dir = std::env::var("OUT_DIR").unwrap();
            let lib_path = format!("{}/libfileprovider_bridge.a", out_dir);

            // Compile as static library (.a) to avoid dylib runtime dependency
            let status = Command::new("swiftc")
                .args([
                    "-emit-library",
                    "-static",
                    "-o",
                    &lib_path,
                    "-module-name",
                    "FileProviderBridge",
                    swift_bridge,
                ])
                .status()
                .expect("Failed to compile Swift bridge");

            if status.success() {
                println!("cargo:rustc-link-search=native={}", out_dir);
                println!("cargo:rustc-link-lib=static=fileprovider_bridge");
                // Link required frameworks
                println!("cargo:rustc-link-lib=framework=FileProvider");
                println!("cargo:rustc-link-lib=framework=Foundation");
                // Swift runtime (needed for static Swift code)
                let swift_lib_dir = String::from_utf8(
                    Command::new("xcrun")
                        .args(["--show-sdk-path"])
                        .output()
                        .expect("xcrun failed")
                        .stdout,
                )
                .unwrap()
                .trim()
                .to_string();
                // Link Swift standard library
                let toolchain_lib = String::from_utf8(
                    Command::new("xcrun")
                        .args(["--toolchain", "default", "--find", "swiftc"])
                        .output()
                        .expect("xcrun failed")
                        .stdout,
                )
                .unwrap()
                .trim()
                .to_string();
                let toolchain_dir =
                    std::path::Path::new(&toolchain_lib).parent().unwrap().parent().unwrap();
                let swift_static_lib =
                    toolchain_dir.join("lib/swift_static/macosx");
                if swift_static_lib.exists() {
                    println!(
                        "cargo:rustc-link-search=native={}",
                        swift_static_lib.display()
                    );
                }
                // Also search in platform SDK
                let platform_swift_lib = format!("{}/usr/lib/swift", swift_lib_dir);
                println!("cargo:rustc-link-search=native={}", platform_swift_lib);
            } else {
                eprintln!(
                    "Warning: Swift bridge compilation failed, File Provider features disabled"
                );
            }
        }
    }

    tauri_build::build()
}
