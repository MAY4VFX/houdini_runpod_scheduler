#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::io::Read as IoRead;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Mutex;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

// ─── Data structures ────────────────────────────────────────────────

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct JuiceFSConfig {
    pub redis_url: String,
    pub b2_endpoint: String,
    pub b2_access_key: String,
    pub b2_secret_key: String,
    pub b2_bucket: String,
    pub rsa_key: String,
    pub project_id: String,
    pub mount_path: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct AppStatus {
    pub connected: bool,
    pub mounted: bool,
    pub project_id: Option<String>,
    pub mount_path: Option<String>,
    pub houdini_found: bool,
    pub hda_installed: bool,
}

impl Default for AppStatus {
    fn default() -> Self {
        Self {
            connected: false,
            mounted: false,
            project_id: None,
            mount_path: None,
            houdini_found: false,
            hda_installed: false,
        }
    }
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct DependencyStatus {
    pub juicefs_installed: bool,
    pub juicefs_path: Option<String>,
    pub juicefs_downloading: bool,
    pub fuse_installed: bool,
    pub fuse_install_url: Option<String>,
    pub fuse_install_instructions: Option<String>,
    pub houdini_found: bool,
    pub all_ready: bool,
}

#[derive(Default)]
struct AppState {
    config: Option<JuiceFSConfig>,
    api_key: Option<String>,
    api_url: Option<String>,
}

// ─── JuiceFS binary management ──────────────────────────────────────

/// Return the base directory for RunPodFarm app data: ~/.runpodfarm
fn app_data_dir() -> PathBuf {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    home.join(".runpodfarm")
}

/// Return the path where juicefs binary should live: ~/.runpodfarm/bin/juicefs
fn juicefs_bin_path() -> PathBuf {
    let bin_dir = app_data_dir().join("bin");
    if cfg!(target_os = "windows") {
        bin_dir.join("juicefs.exe")
    } else {
        bin_dir.join("juicefs")
    }
}

/// Check if juicefs binary is available (in our app dir or on PATH).
fn check_juicefs_installed_sync() -> bool {
    // First check our own managed binary
    if juicefs_bin_path().exists() {
        return true;
    }
    // Then check system PATH
    if cfg!(target_os = "windows") {
        Command::new("where")
            .arg("juicefs")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    } else {
        Command::new("which")
            .arg("juicefs")
            .output()
            .map(|o| o.status.success())
            .unwrap_or(false)
    }
}

/// Return the path to the juicefs binary (prefer managed, then system).
fn get_juicefs_path_sync() -> Option<String> {
    let managed = juicefs_bin_path();
    if managed.exists() {
        return Some(managed.to_string_lossy().to_string());
    }
    // Try system PATH
    let cmd = if cfg!(target_os = "windows") {
        "where"
    } else {
        "which"
    };
    Command::new(cmd)
        .arg("juicefs")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| {
            String::from_utf8(o.stdout)
                .ok()
                .map(|s| s.trim().to_string())
        })
}

/// Return the download URL for the current platform.
fn juicefs_download_url() -> &'static str {
    if cfg!(target_os = "macos") && cfg!(target_arch = "aarch64") {
        "https://github.com/juicedata/juicefs/releases/download/v1.2.0/juicefs-1.2.0-darwin-arm64.tar.gz"
    } else if cfg!(target_os = "macos") && cfg!(target_arch = "x86_64") {
        "https://github.com/juicedata/juicefs/releases/download/v1.2.0/juicefs-1.2.0-darwin-amd64.tar.gz"
    } else if cfg!(target_os = "linux") && cfg!(target_arch = "x86_64") {
        "https://github.com/juicedata/juicefs/releases/download/v1.2.0/juicefs-1.2.0-linux-amd64.tar.gz"
    } else if cfg!(target_os = "windows") && cfg!(target_arch = "x86_64") {
        "https://github.com/juicedata/juicefs/releases/download/v1.2.0/juicefs-1.2.0-windows-amd64.zip"
    } else {
        // Fallback to linux amd64
        "https://github.com/juicedata/juicefs/releases/download/v1.2.0/juicefs-1.2.0-linux-amd64.tar.gz"
    }
}

/// Download and install the JuiceFS binary into ~/.runpodfarm/bin/
async fn download_juicefs_impl() -> Result<String, String> {
    let url = juicefs_download_url();
    let bin_dir = app_data_dir().join("bin");
    std::fs::create_dir_all(&bin_dir)
        .map_err(|e| format!("Failed to create bin dir: {}", e))?;

    // Download the archive
    let client = reqwest::Client::new();
    let resp = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("Download failed: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("Download failed with status: {}", resp.status()));
    }

    let bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("Failed to read download: {}", e))?;

    let dest = juicefs_bin_path();
    let is_zip = url.ends_with(".zip");

    if is_zip {
        // Windows: extract from zip
        extract_juicefs_from_zip(&bytes, &dest)?;
    } else {
        // Unix: extract from tar.gz
        extract_juicefs_from_targz(&bytes, &dest)?;
    }

    // Make executable on Unix
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&dest, std::fs::Permissions::from_mode(0o755))
            .map_err(|e| format!("Failed to set permissions: {}", e))?;
    }

    Ok(dest.to_string_lossy().to_string())
}

/// Extract the juicefs binary from a tar.gz archive.
fn extract_juicefs_from_targz(data: &[u8], dest: &PathBuf) -> Result<(), String> {
    let gz = flate2::read::GzDecoder::new(data);
    let mut archive = tar::Archive::new(gz);

    let entries = archive
        .entries()
        .map_err(|e| format!("Failed to read tar entries: {}", e))?;

    for entry_result in entries {
        let mut entry = entry_result.map_err(|e| format!("Failed to read entry: {}", e))?;
        let path = entry
            .path()
            .map_err(|e| format!("Failed to read entry path: {}", e))?
            .to_path_buf();

        let file_name = path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default();

        if file_name == "juicefs" {
            let mut contents = Vec::new();
            entry
                .read_to_end(&mut contents)
                .map_err(|e| format!("Failed to read juicefs from archive: {}", e))?;
            std::fs::write(dest, &contents)
                .map_err(|e| format!("Failed to write juicefs binary: {}", e))?;
            return Ok(());
        }
    }

    Err("juicefs binary not found in archive".to_string())
}

/// Extract the juicefs binary from a zip archive (Windows).
fn extract_juicefs_from_zip(data: &[u8], dest: &PathBuf) -> Result<(), String> {
    let reader = std::io::Cursor::new(data);
    let mut archive =
        zip::ZipArchive::new(reader).map_err(|e| format!("Failed to open zip: {}", e))?;

    for i in 0..archive.len() {
        let mut file = archive
            .by_index(i)
            .map_err(|e| format!("Failed to read zip entry: {}", e))?;
        let name = file.name().to_string();

        if name.ends_with("juicefs.exe") || name.ends_with("juicefs") {
            let mut contents = Vec::new();
            file.read_to_end(&mut contents)
                .map_err(|e| format!("Failed to read juicefs from zip: {}", e))?;
            std::fs::write(dest, &contents)
                .map_err(|e| format!("Failed to write juicefs binary: {}", e))?;
            return Ok(());
        }
    }

    Err("juicefs binary not found in zip archive".to_string())
}

// ─── FUSE driver check ──────────────────────────────────────────────

fn check_fuse_installed_sync() -> bool {
    if cfg!(target_os = "macos") {
        // Check for macFUSE
        std::path::Path::new("/Library/Filesystems/macfuse.fs").exists()
            || std::path::Path::new("/usr/local/lib/libfuse.dylib").exists()
            || std::path::Path::new("/Library/Frameworks/macFUSE.framework").exists()
    } else if cfg!(target_os = "linux") {
        // Check for fuse3 / fuse
        std::path::Path::new("/dev/fuse").exists()
            || Command::new("which")
                .arg("fusermount3")
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false)
            || Command::new("which")
                .arg("fusermount")
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false)
    } else if cfg!(target_os = "windows") {
        // Check for WinFsp
        std::path::Path::new("C:\\Program Files\\WinFsp").exists()
            || std::path::Path::new("C:\\Program Files (x86)\\WinFsp").exists()
    } else {
        false
    }
}

fn get_fuse_install_url() -> String {
    if cfg!(target_os = "macos") {
        "https://github.com/osxfuse/osxfuse/releases".to_string()
    } else if cfg!(target_os = "windows") {
        "https://github.com/winfsp/winfsp/releases".to_string()
    } else {
        "https://packages.ubuntu.com/fuse3".to_string()
    }
}

fn get_fuse_install_instructions() -> String {
    if cfg!(target_os = "macos") {
        "Install macFUSE from the link above. After installing, you may need to allow the kernel extension in System Settings > Privacy & Security, then restart your Mac.".to_string()
    } else if cfg!(target_os = "windows") {
        "Install WinFsp from the link above. Run the installer and follow the prompts. A reboot may be required.".to_string()
    } else {
        "Install FUSE with: sudo apt install -y fuse3".to_string()
    }
}

// ─── FUSE auto-install ───────────────────────────────────────────────

async fn install_fuse_macos() -> Result<String, String> {
    // Find Homebrew binary — check common paths directly since Tauri subprocesses
    // often don't have /opt/homebrew/bin in PATH
    let brew_path = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
        .iter()
        .find(|p| std::path::Path::new(p).exists())
        .map(|p| p.to_string());

    let brew = match brew_path {
        Some(b) => b,
        None => {
            return Err(
                "Homebrew not found. Install it from https://brew.sh first.".to_string(),
            );
        }
    };

    // Quick check: already installed?
    let list_out = Command::new(&brew)
        .args(["list", "--cask", "macfuse"])
        .output()
        .ok();
    if list_out.map(|o| o.status.success()).unwrap_or(false) {
        return Ok("macFUSE is already installed.".to_string());
    }

    // Create a temporary askpass script that shows a native macOS password dialog.
    // When brew needs sudo (no tty available), sudo uses SUDO_ASKPASS automatically.
    let askpass_path = std::env::temp_dir().join("runpodfarm-askpass.sh");
    std::fs::write(
        &askpass_path,
        "#!/bin/bash\nosascript -e 'display dialog \"RunPodFarm needs your password to install macFUSE:\" default answer \"\" with hidden answer buttons {\"OK\",\"Cancel\"} default button \"OK\" with icon caution with title \"RunPodFarm\"' -e 'text returned of result' 2>/dev/null\n",
    )
    .map_err(|e| format!("Failed to create askpass script: {}", e))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&askpass_path, std::fs::Permissions::from_mode(0o755))
            .map_err(|e| format!("Failed to chmod askpass: {}", e))?;
    }

    // Run brew install with SUDO_ASKPASS — shows native macOS password dialog if needed
    let output = Command::new(&brew)
        .args(["install", "--cask", "macfuse"])
        .env("SUDO_ASKPASS", &askpass_path)
        .output()
        .map_err(|e| format!("Failed to run brew: {}", e))?;

    // Clean up askpass script
    let _ = std::fs::remove_file(&askpass_path);

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);

    if output.status.success() || stdout.contains("already installed") || stderr.contains("already installed") {
        return Ok("macFUSE installed. You may need to approve the system extension in System Settings > Privacy & Security.".to_string());
    }

    Err(format!(
        "brew install failed (exit {}): {}",
        output.status.code().unwrap_or(-1),
        stderr.chars().take(300).collect::<String>()
    ))
}

fn install_fuse_linux() -> Result<String, String> {
    let output = Command::new("pkexec")
        .args(["apt", "install", "-y", "fuse3"])
        .output()
        .map_err(|e| format!("Failed to install fuse3: {}", e))?;

    if output.status.success() {
        Ok("FUSE installed successfully.".to_string())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!(
            "Failed to install FUSE: {}. Try manually: sudo apt install -y fuse3",
            stderr
        ))
    }
}

async fn install_fuse_windows() -> Result<String, String> {
    let msi_url =
        "https://github.com/winfsp/winfsp/releases/download/v2.0/winfsp-2.0.23075.msi";

    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::limited(10))
        .build()
        .map_err(|e| format!("HTTP client error: {}", e))?;

    let resp = client
        .get(msi_url)
        .send()
        .await
        .map_err(|e| format!("Failed to download WinFsp: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("Download failed: HTTP {}", resp.status()));
    }

    let bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("Failed to read download: {}", e))?;

    let tmp_path = std::env::temp_dir().join("winfsp-install.msi");
    std::fs::write(&tmp_path, &bytes)
        .map_err(|e| format!("Failed to save installer: {}", e))?;

    Command::new("msiexec")
        .args(["/i", &tmp_path.to_string_lossy(), "/passive"])
        .output()
        .map_err(|e| format!("Failed to run installer: {}", e))?;

    Ok("WinFsp installer launched. Please complete the installation, then reboot if prompted."
        .to_string())
}

// ─── Houdini helpers ────────────────────────────────────────────────

/// Scan for Houdini installations dynamically (macOS: /Applications/Houdini/Houdini*).
/// Returns (resources_path, major_minor_version) pairs sorted newest-first.
fn scan_houdini_macos() -> Vec<(String, String)> {
    let base = std::path::Path::new("/Applications/Houdini");
    if !base.exists() {
        return vec![];
    }
    let mut found: Vec<(String, String)> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(base) {
        let mut dirs: Vec<String> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let name = e.file_name().to_string_lossy().to_string();
                if name.starts_with("Houdini") && name != "Houdini" && e.path().is_dir() {
                    Some(name)
                } else {
                    None
                }
            })
            .collect();
        dirs.sort_by(|a, b| b.cmp(a));
        for dir_name in &dirs {
            let version_str = dir_name.trim_start_matches("Houdini");
            let parts: Vec<&str> = version_str.splitn(3, '.').collect();
            let major_minor = if parts.len() >= 2 {
                format!("{}.{}", parts[0], parts[1])
            } else {
                version_str.to_string()
            };
            if major_minor.is_empty() {
                continue;
            }
            let resources = base
                .join(dir_name)
                .join("Frameworks/Houdini.framework/Versions")
                .join(&major_minor)
                .join("Resources");
            if resources.exists() {
                found.push((resources.to_string_lossy().to_string(), major_minor));
            }
        }
    }
    // Also check "Current" symlink
    let current = base.join("Current/Frameworks/Houdini.framework/Versions/Current/Resources");
    if current.exists() {
        let current_str = current.to_string_lossy().to_string();
        if !found.iter().any(|(p, _)| p == &current_str) {
            found.push((current_str, "Current".to_string()));
        }
    }
    found
}

fn scan_houdini_linux() -> Vec<(String, String)> {
    let base = std::path::Path::new("/opt");
    if !base.exists() {
        return vec![];
    }
    let mut found: Vec<(String, String)> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(base) {
        let mut dirs: Vec<String> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let name = e.file_name().to_string_lossy().to_string();
                if name.starts_with("hfs") && e.path().is_dir() {
                    Some(name)
                } else {
                    None
                }
            })
            .collect();
        dirs.sort_by(|a, b| b.cmp(a));
        for dir_name in &dirs {
            let version = dir_name.trim_start_matches("hfs").to_string();
            let path = base.join(dir_name).to_string_lossy().to_string();
            found.push((path, version));
        }
    }
    found
}

fn scan_houdini_windows() -> Vec<(String, String)> {
    let base = std::path::Path::new("C:\\Program Files\\Side Effects Software");
    if !base.exists() {
        return vec![];
    }
    let mut found: Vec<(String, String)> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(base) {
        let mut dirs: Vec<String> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let name = e.file_name().to_string_lossy().to_string();
                if name.starts_with("Houdini ") && e.path().is_dir() {
                    Some(name)
                } else {
                    None
                }
            })
            .collect();
        dirs.sort_by(|a, b| b.cmp(a));
        for dir_name in &dirs {
            let version = dir_name.trim_start_matches("Houdini ").to_string();
            let path = base.join(dir_name).to_string_lossy().to_string();
            found.push((path, version));
        }
    }
    found
}

/// Scan all Houdini installations, returns (path, version) pairs newest-first.
fn scan_all_houdini() -> Vec<(String, String)> {
    if cfg!(target_os = "macos") {
        scan_houdini_macos()
    } else if cfg!(target_os = "linux") {
        scan_houdini_linux()
    } else {
        scan_houdini_windows()
    }
}

fn find_houdini() -> Option<String> {
    scan_all_houdini().into_iter().next().map(|(path, _)| path)
}

/// Find Houdini and return (path, version).
fn find_houdini_with_version() -> Option<(String, String)> {
    scan_all_houdini().into_iter().next()
}

fn find_all_houdini() -> Vec<String> {
    scan_all_houdini().into_iter().map(|(path, _)| path).collect()
}

/// Get the otls directory for a given Houdini version.
fn get_otls_dir_for_version(version: &str) -> String {
    if cfg!(target_os = "macos") {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/unknown".to_string());
        format!("{}/Library/Preferences/houdini/{}/otls", home, version)
    } else if cfg!(target_os = "linux") {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/home/unknown".to_string());
        format!("{}/houdini{}/otls", home, version)
    } else {
        let userprofile =
            std::env::var("USERPROFILE").unwrap_or_else(|_| "C:\\Users\\unknown".to_string());
        format!("{}\\Documents\\houdini{}\\otls", userprofile, version)
    }
}

fn get_otls_dir(houdini_path: &str) -> String {
    // Extract version from the Houdini path
    let version = scan_all_houdini()
        .into_iter()
        .find(|(p, _)| p == houdini_path)
        .map(|(_, v)| v)
        .unwrap_or_else(|| "20.5".to_string());
    get_otls_dir_for_version(&version)
}

fn check_hda_installed(houdini_path: &str) -> bool {
    let otls_dir = get_otls_dir(houdini_path);
    std::path::Path::new(&format!("{}/runpodfarm_scheduler.hda", otls_dir)).exists()
}

fn check_mount(path: &str) -> bool {
    let path_exists = std::path::Path::new(path).exists();
    if !path_exists {
        return false;
    }

    if cfg!(target_os = "macos") || cfg!(target_os = "linux") {
        Command::new("mountpoint")
            .args(["-q", path])
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    } else {
        path_exists
    }
}

// ─── Tauri commands ─────────────────────────────────────────────────

#[tauri::command]
async fn check_juicefs_installed() -> Result<bool, String> {
    Ok(check_juicefs_installed_sync())
}

#[tauri::command]
async fn get_juicefs_path() -> Result<Option<String>, String> {
    Ok(get_juicefs_path_sync())
}

#[tauri::command]
async fn download_juicefs() -> Result<String, String> {
    download_juicefs_impl().await
}

#[tauri::command]
async fn check_fuse_installed() -> Result<bool, String> {
    Ok(check_fuse_installed_sync())
}

#[tauri::command]
async fn install_fuse() -> Result<String, String> {
    if cfg!(target_os = "macos") {
        install_fuse_macos().await
    } else if cfg!(target_os = "linux") {
        // Wrap sync fn in async context
        install_fuse_linux()
    } else if cfg!(target_os = "windows") {
        install_fuse_windows().await
    } else {
        Err("Unsupported platform".to_string())
    }
}

#[tauri::command]
async fn ensure_dependencies() -> Result<DependencyStatus, String> {
    let juicefs_installed = check_juicefs_installed_sync();
    let juicefs_path = get_juicefs_path_sync();
    let fuse_installed = check_fuse_installed_sync();
    let houdini_found = find_houdini().is_some();

    let (fuse_url, fuse_instructions) = if !fuse_installed {
        (
            Some(get_fuse_install_url()),
            Some(get_fuse_install_instructions()),
        )
    } else {
        (None, None)
    };

    let all_ready = juicefs_installed && fuse_installed;

    Ok(DependencyStatus {
        juicefs_installed,
        juicefs_path,
        juicefs_downloading: false,
        fuse_installed,
        fuse_install_url: fuse_url,
        fuse_install_instructions: fuse_instructions,
        houdini_found,
        all_ready,
    })
}

#[tauri::command]
async fn connect(
    api_key: String,
    api_url: String,
    mount_path: Option<String>,
    state: tauri::State<'_, Mutex<AppState>>,
) -> Result<AppStatus, String> {
    // 1. Check FUSE
    if !check_fuse_installed_sync() {
        return Err(format!(
            "FUSE driver not installed. Please install it first: {}",
            get_fuse_install_url()
        ));
    }

    // 2. Check JuiceFS -- auto-download if missing
    if !check_juicefs_installed_sync() {
        download_juicefs_impl().await?;
    }

    // 3. Auth API call — GET /api/artist/config with X-API-Key
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("{}/api/artist/config", api_url))
        .header("X-API-Key", &api_key)
        .send()
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(format!("Auth failed ({}): {}", status, body));
    }

    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Failed to parse response: {}", e))?;

    let cfg = body.get("config").ok_or("No config in server response")?;

    let project_id_str = cfg.get("project_id").and_then(|v| v.as_str()).unwrap_or("default").to_string();

    // Use user-provided mount_path, or default to ~/RunPodFarm/{project_id}
    let local_mount_path = mount_path.unwrap_or_else(|| {
        dirs::home_dir()
            .unwrap_or_else(|| std::path::PathBuf::from("/tmp"))
            .join("RunPodFarm")
            .join(&project_id_str)
            .to_string_lossy()
            .to_string()
    });

    let config = JuiceFSConfig {
        redis_url: cfg.get("redis_url").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        b2_endpoint: cfg.get("b2_endpoint").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        b2_access_key: cfg.get("b2_access_key").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        b2_secret_key: cfg.get("b2_secret_key").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        b2_bucket: cfg.get("b2_bucket").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        rsa_key: cfg.get("rsa_key").and_then(|v| v.as_str()).unwrap_or("").to_string(),
        project_id: project_id_str,
        mount_path: local_mount_path,
    };

    let project_id = config.project_id.clone();
    let mount_path = config.mount_path.clone();

    {
        let mut app_state = state.lock().map_err(|e| e.to_string())?;
        app_state.config = Some(config);
        app_state.api_key = Some(api_key);
        app_state.api_url = Some(api_url);
    }

    // 4. Auto-mount JuiceFS with retry (only if storage config is available)
    let has_storage_config = {
        let app_state = state.lock().map_err(|e| e.to_string())?;
        app_state
            .config
            .as_ref()
            .map(|c| !c.redis_url.is_empty())
            .unwrap_or(false)
    };
    let mut mounted = false;
    let mut mount_error = String::new();
    if has_storage_config {
        for attempt in 1..=3 {
            match mount_juicefs_inner(&state).await {
                Ok(_) => {
                    mounted = true;
                    break;
                }
                Err(e) => {
                    mount_error = e;
                    if attempt < 3 {
                        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                    }
                }
            }
        }
        if !mounted {
            return Err(format!("Connected but mount failed: {}", mount_error));
        }
    }

    // 5. Check Houdini and HDA
    let houdini_found = find_houdini().is_some();
    let hda_installed = find_houdini()
        .as_ref()
        .map(|p| check_hda_installed(p))
        .unwrap_or(false);

    Ok(AppStatus {
        connected: true,
        mounted,
        project_id: Some(project_id),
        mount_path: if mounted { Some(mount_path) } else { None },
        houdini_found,
        hda_installed,
    })
}

#[tauri::command]
async fn disconnect(state: tauri::State<'_, Mutex<AppState>>) -> Result<(), String> {
    let (mount_path, juicefs_path) = {
        let app_state = state.lock().map_err(|e| e.to_string())?;
        let mp = app_state
            .config
            .as_ref()
            .map(|c| c.mount_path.clone())
            .unwrap_or_else(|| "/project".to_string());
        let jp = get_juicefs_path_sync().unwrap_or_else(|| "juicefs".to_string());
        (mp, jp)
    };

    let _ = Command::new(&juicefs_path)
        .args(["umount", &mount_path])
        .output();

    {
        let mut app_state = state.lock().map_err(|e| e.to_string())?;
        app_state.config = None;
        app_state.api_key = None;
        app_state.api_url = None;
    }

    Ok(())
}

#[tauri::command]
async fn get_status(state: tauri::State<'_, Mutex<AppState>>) -> Result<AppStatus, String> {
    let app_state = state.lock().map_err(|e| e.to_string())?;

    let (connected, project_id, mount_path_str) = match &app_state.config {
        Some(config) => (true, Some(config.project_id.clone()), config.mount_path.clone()),
        None => (false, None, "/project".to_string()),
    };

    let mounted = check_mount(&mount_path_str);
    let houdini_path = find_houdini();
    let hda_installed = houdini_path
        .as_ref()
        .map(|p| check_hda_installed(p))
        .unwrap_or(false);

    Ok(AppStatus {
        connected,
        mounted,
        project_id,
        mount_path: if mounted {
            Some(mount_path_str)
        } else {
            None
        },
        houdini_found: houdini_path.is_some(),
        hda_installed,
    })
}

/// Internal helper for mount_juicefs that takes a reference to State
async fn mount_juicefs_inner(
    state: &tauri::State<'_, Mutex<AppState>>,
) -> Result<String, String> {
    let config = {
        let app_state = state.lock().map_err(|e| e.to_string())?;
        app_state
            .config
            .clone()
            .ok_or_else(|| "Not connected".to_string())?
    };

    let juicefs_path = get_juicefs_path_sync()
        .ok_or_else(|| "JuiceFS binary not found. Please download it first.".to_string())?;

    // Create mount directory if it doesn't exist
    std::fs::create_dir_all(&config.mount_path)
        .map_err(|e| format!("Failed to create mount dir: {}", e))?;

    // JuiceFS mount reads storage config from Redis metadata (set during juicefs format).
    // Only pass credentials as env vars if they look real (not placeholders).
    let mut cmd = Command::new(&juicefs_path);
    cmd.args(["mount", &config.redis_url, &config.mount_path, "-d"]);

    let is_real_key = |k: &str| !k.is_empty() && !k.starts_with("test-") && k.len() > 10;
    if is_real_key(&config.b2_access_key) {
        cmd.env("ACCESS_KEY", &config.b2_access_key);
    }
    if is_real_key(&config.b2_secret_key) {
        cmd.env("SECRET_KEY", &config.b2_secret_key);
    }

    let output = cmd.output()
        .map_err(|e| format!("Failed to run juicefs: {}", e))?;

    if output.status.success() {
        Ok(config.mount_path)
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!("JuiceFS mount failed: {}", stderr))
    }
}

#[tauri::command]
async fn mount_juicefs(state: tauri::State<'_, Mutex<AppState>>) -> Result<String, String> {
    mount_juicefs_inner(&state).await
}

#[tauri::command]
async fn unmount_juicefs(state: tauri::State<'_, Mutex<AppState>>) -> Result<(), String> {
    let mount_path = {
        let app_state = state.lock().map_err(|e| e.to_string())?;
        app_state
            .config
            .as_ref()
            .map(|c| c.mount_path.clone())
            .unwrap_or_else(|| "/project".to_string())
    };

    let juicefs_path = get_juicefs_path_sync()
        .ok_or_else(|| "JuiceFS binary not found".to_string())?;

    let output = Command::new(&juicefs_path)
        .args(["umount", &mount_path])
        .output()
        .map_err(|e| format!("Failed to run juicefs umount: {}", e))?;

    if output.status.success() {
        Ok(())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!("JuiceFS unmount failed: {}", stderr))
    }
}

/// Recursively copy a directory and all its contents.
fn copy_dir_recursive(src: &std::path::Path, dst: &std::path::Path) -> Result<(), String> {
    if !src.is_dir() {
        return Err(format!("Source is not a directory: {}", src.display()));
    }
    std::fs::create_dir_all(dst)
        .map_err(|e| format!("Failed to create directory {}: {}", dst.display(), e))?;

    let entries = std::fs::read_dir(src)
        .map_err(|e| format!("Failed to read directory {}: {}", src.display(), e))?;

    for entry in entries {
        let entry = entry.map_err(|e| format!("Failed to read dir entry: {}", e))?;
        let src_path = entry.path();
        let dst_path = dst.join(entry.file_name());

        if src_path.is_dir() {
            copy_dir_recursive(&src_path, &dst_path)?;
        } else {
            std::fs::copy(&src_path, &dst_path).map_err(|e| {
                format!(
                    "Failed to copy {} -> {}: {}",
                    src_path.display(),
                    dst_path.display(),
                    e
                )
            })?;
        }
    }
    Ok(())
}

/// Find the HDA source directory by checking known locations.
fn find_hda_source() -> Option<PathBuf> {
    let hda_name = "runpodfarm_scheduler.hda";

    // 1. Check relative to the executable (in a repo checkout: ../../../hda/)
    if let Ok(exe_path) = std::env::current_exe() {
        // Walk up from the executable to find the repo root
        let mut dir = exe_path.parent().map(|p| p.to_path_buf());
        for _ in 0..6 {
            if let Some(ref d) = dir {
                let candidate = d.join("hda").join(hda_name);
                if candidate.is_dir() {
                    return Some(candidate);
                }
                dir = d.parent().map(|p| p.to_path_buf());
            }
        }
    }

    // 2. Check common repo locations relative to home directory
    let home = dirs::home_dir()?;
    let candidates = vec![
        home.join("houdini_runpod_scheduler/hda").join(hda_name),
        home.join("houdini_runpod_scheduler/.worktrees/runpodfarm/hda").join(hda_name),
        home.join("Projects/houdini_runpod_scheduler/hda").join(hda_name),
        home.join("dev/houdini_runpod_scheduler/hda").join(hda_name),
    ];

    candidates.into_iter().find(|p| p.is_dir())
}

#[tauri::command]
async fn install_hda(app: tauri::AppHandle) -> Result<String, String> {
    let houdini_path =
        find_houdini().ok_or_else(|| "Houdini installation not found".to_string())?;

    let otls_dir = get_otls_dir(&houdini_path);
    std::fs::create_dir_all(&otls_dir)
        .map_err(|e| format!("Failed to create otls dir: {}", e))?;

    // Try bundled resources first (production build), then fall back to disk search (dev)
    // Tauri converts ../../ to _up_/_up_/ in the resource path
    let hda_source = app
        .path()
        .resource_dir()
        .ok()
        .and_then(|r| {
            // Try the Tauri-mangled path first (_up_/_up_/hda/...)
            let mangled = r.join("_up_/_up_/hda/runpodfarm_scheduler.hda");
            if mangled.is_dir() {
                return Some(mangled);
            }
            // Try direct path
            let direct = r.join("runpodfarm_scheduler.hda");
            if direct.is_dir() {
                return Some(direct);
            }
            // Try hda/ subdirectory
            let hda_sub = r.join("hda/runpodfarm_scheduler.hda");
            if hda_sub.is_dir() {
                return Some(hda_sub);
            }
            None
        })
        .or_else(find_hda_source)
        .ok_or_else(|| {
            "HDA not found in app resources or on disk".to_string()
        })?;

    let hda_dest = PathBuf::from(&otls_dir).join("runpodfarm_scheduler.hda");

    // Remove existing HDA if present, to ensure a clean copy
    if hda_dest.exists() {
        std::fs::remove_dir_all(&hda_dest)
            .map_err(|e| format!("Failed to remove existing HDA: {}", e))?;
    }

    copy_dir_recursive(&hda_source, &hda_dest)?;

    Ok(format!(
        "HDA installed successfully to: {}",
        hda_dest.display()
    ))
}

#[tauri::command]
async fn get_houdini_info() -> Result<serde_json::Value, String> {
    let installations = find_all_houdini();
    Ok(serde_json::json!({
        "installations": installations,
        "primary": find_houdini(),
    }))
}

// ─── Entry point ────────────────────────────────────────────────────

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .manage(Mutex::new(AppState::default()))
        .setup(|app| {
            // Build tray menu
            let show_i = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
            let status_i =
                MenuItem::with_id(app, "status", "Status: Disconnected", false, None::<&str>)?;
            let disconnect_i =
                MenuItem::with_id(app, "disconnect", "Disconnect", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;

            let menu = Menu::with_items(app, &[&show_i, &status_i, &disconnect_i, &quit_i])?;

            let _tray = TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("RunPodFarm")
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "disconnect" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.eval(
                                "window.__trayDisconnect && window.__trayDisconnect()",
                            );
                        }
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            connect,
            disconnect,
            get_status,
            mount_juicefs,
            unmount_juicefs,
            install_hda,
            get_houdini_info,
            check_juicefs_installed,
            get_juicefs_path,
            download_juicefs,
            check_fuse_installed,
            install_fuse,
            ensure_dependencies,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
