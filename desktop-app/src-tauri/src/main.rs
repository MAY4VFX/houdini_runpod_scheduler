#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::{Deserialize, Serialize};
use std::process::Command;
use std::sync::Mutex;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

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

#[derive(Default)]
struct AppState {
    config: Option<JuiceFSConfig>,
    api_key: Option<String>,
    api_url: Option<String>,
}

#[tauri::command]
async fn connect(
    api_key: String,
    api_url: String,
    state: tauri::State<'_, Mutex<AppState>>,
) -> Result<AppStatus, String> {
    let client = reqwest::Client::new();
    let resp = client
        .get(format!("{}/projects/default/config", api_url))
        .header("X-API-Key", &api_key)
        .send()
        .await
        .map_err(|e| format!("Request failed: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("Auth failed: {}", resp.status()));
    }

    let config: JuiceFSConfig = resp
        .json()
        .await
        .map_err(|e| format!("Failed to parse config: {}", e))?;

    let project_id = config.project_id.clone();
    let mount_path = config.mount_path.clone();

    {
        let mut app_state = state.lock().map_err(|e| e.to_string())?;
        app_state.config = Some(config);
        app_state.api_key = Some(api_key);
        app_state.api_url = Some(api_url);
    }

    Ok(AppStatus {
        connected: true,
        mounted: false,
        project_id: Some(project_id),
        mount_path: Some(mount_path),
        houdini_found: find_houdini().is_some(),
        hda_installed: false,
    })
}

#[tauri::command]
async fn disconnect(state: tauri::State<'_, Mutex<AppState>>) -> Result<(), String> {
    let mount_path = {
        let app_state = state.lock().map_err(|e| e.to_string())?;
        app_state
            .config
            .as_ref()
            .map(|c| c.mount_path.clone())
            .unwrap_or_else(|| "/project".to_string())
    };

    let _ = Command::new("juicefs")
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

#[tauri::command]
async fn mount_juicefs(state: tauri::State<'_, Mutex<AppState>>) -> Result<String, String> {
    let config = {
        let app_state = state.lock().map_err(|e| e.to_string())?;
        app_state
            .config
            .clone()
            .ok_or_else(|| "Not connected".to_string())?
    };

    // Create mount directory if it doesn't exist
    std::fs::create_dir_all(&config.mount_path)
        .map_err(|e| format!("Failed to create mount dir: {}", e))?;

    let storage_url = format!(
        "{}://{}/{}",
        "s3", config.b2_endpoint, config.b2_bucket
    );

    let output = Command::new("juicefs")
        .args([
            "mount",
            &config.redis_url,
            &config.mount_path,
            "--storage",
            "s3",
            "--bucket",
            &storage_url,
            "--access-key",
            &config.b2_access_key,
            "--secret-key",
            &config.b2_secret_key,
            "-d",
        ])
        .output()
        .map_err(|e| format!("Failed to run juicefs: {}", e))?;

    if output.status.success() {
        Ok(config.mount_path)
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!("JuiceFS mount failed: {}", stderr))
    }
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

    let output = Command::new("juicefs")
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

#[tauri::command]
async fn install_hda() -> Result<String, String> {
    let houdini_path =
        find_houdini().ok_or_else(|| "Houdini installation not found".to_string())?;

    let otls_dir = get_otls_dir(&houdini_path);
    std::fs::create_dir_all(&otls_dir)
        .map_err(|e| format!("Failed to create otls dir: {}", e))?;

    // HDA source path relative to app resources
    let hda_source = "runpodfarm_scheduler.hda";
    let hda_dest = format!("{}/runpodfarm_scheduler.hda", otls_dir);

    // For now, return info about where HDA would be installed
    Ok(format!(
        "HDA would be installed to: {} (source: {})",
        hda_dest, hda_source
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

fn find_houdini() -> Option<String> {
    let paths = if cfg!(target_os = "macos") {
        vec![
            "/Applications/Houdini/Current/Frameworks/Houdini.framework/Versions/Current/Resources",
            "/Applications/Houdini/Houdini20.5/Frameworks/Houdini.framework/Versions/20.5/Resources",
            "/Applications/Houdini/Houdini20.0/Frameworks/Houdini.framework/Versions/20.0/Resources",
        ]
    } else if cfg!(target_os = "linux") {
        vec!["/opt/hfs20.5", "/opt/hfs20.0", "/opt/hfs19.5"]
    } else {
        vec![
            "C:\\Program Files\\Side Effects Software\\Houdini 20.5",
            "C:\\Program Files\\Side Effects Software\\Houdini 20.0",
        ]
    };

    paths
        .into_iter()
        .find(|p| std::path::Path::new(p).exists())
        .map(|p| p.to_string())
}

fn find_all_houdini() -> Vec<String> {
    let paths = if cfg!(target_os = "macos") {
        vec![
            "/Applications/Houdini/Current/Frameworks/Houdini.framework/Versions/Current/Resources",
            "/Applications/Houdini/Houdini20.5/Frameworks/Houdini.framework/Versions/20.5/Resources",
            "/Applications/Houdini/Houdini20.0/Frameworks/Houdini.framework/Versions/20.0/Resources",
        ]
    } else if cfg!(target_os = "linux") {
        vec!["/opt/hfs20.5", "/opt/hfs20.0", "/opt/hfs19.5"]
    } else {
        vec![
            "C:\\Program Files\\Side Effects Software\\Houdini 20.5",
            "C:\\Program Files\\Side Effects Software\\Houdini 20.0",
        ]
    };

    paths
        .into_iter()
        .filter(|p| std::path::Path::new(p).exists())
        .map(|p| p.to_string())
        .collect()
}

fn get_otls_dir(houdini_path: &str) -> String {
    if cfg!(target_os = "macos") {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/unknown".to_string());
        format!("{}/Library/Preferences/houdini/20.5/otls", home)
    } else if cfg!(target_os = "linux") {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/home/unknown".to_string());
        format!("{}/houdini20.5/otls", home)
    } else {
        let userprofile =
            std::env::var("USERPROFILE").unwrap_or_else(|_| "C:\\Users\\unknown".to_string());
        format!("{}\\Documents\\houdini20.5\\otls", userprofile)
    }
}

fn check_hda_installed(_houdini_path: &str) -> bool {
    let otls_dir = if cfg!(target_os = "macos") {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/unknown".to_string());
        format!("{}/Library/Preferences/houdini/20.5/otls", home)
    } else if cfg!(target_os = "linux") {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/home/unknown".to_string());
        format!("{}/houdini20.5/otls", home)
    } else {
        let userprofile =
            std::env::var("USERPROFILE").unwrap_or_else(|_| "C:\\Users\\unknown".to_string());
        format!("{}\\Documents\\houdini20.5\\otls", userprofile)
    };

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

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .manage(Mutex::new(AppState::default()))
        .setup(|app| {
            // Build tray menu
            let show_i = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
            let status_i = MenuItem::with_id(app, "status", "Status: Disconnected", false, None::<&str>)?;
            let disconnect_i = MenuItem::with_id(app, "disconnect", "Disconnect", true, None::<&str>)?;
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
                            let _ = window.eval("window.__trayDisconnect && window.__trayDisconnect()");
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
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
