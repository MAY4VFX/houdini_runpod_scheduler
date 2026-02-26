import { invoke } from "@tauri-apps/api/core";

export interface AppStatus {
  connected: boolean;
  mounted: boolean;
  project_id: string | null;
  mount_path: string | null;
  houdini_found: boolean;
  hda_installed: boolean;
}

export interface DependencyStatus {
  juicefs_installed: boolean;
  juicefs_path: string | null;
  juicefs_downloading: boolean;
  fuse_installed: boolean;
  fuse_install_url: string | null;
  fuse_install_instructions: string | null;
  houdini_found: boolean;
  all_ready: boolean;
}

export interface HoudiniInfo {
  installations: string[];
  primary: string | null;
}

export interface GatewayStatus {
  running: boolean;
  port: number;
  pid: number | null;
}

// Dependency management
export async function ensureDependencies(): Promise<DependencyStatus> {
  return invoke("ensure_dependencies");
}

export async function checkJuicefsInstalled(): Promise<boolean> {
  return invoke("check_juicefs_installed");
}

export async function getJuicefsPath(): Promise<string | null> {
  return invoke("get_juicefs_path");
}

export async function downloadJuicefs(): Promise<string> {
  return invoke("download_juicefs");
}

export async function checkFuseInstalled(): Promise<boolean> {
  return invoke("check_fuse_installed");
}

export async function installFuse(): Promise<string> {
  return invoke("install_fuse");
}

// Connection
export async function connect(
  apiKey: string,
  apiUrl: string,
  mountPath?: string
): Promise<AppStatus> {
  return invoke("connect", { apiKey, apiUrl, mountPath: mountPath || null });
}

export async function disconnect(): Promise<void> {
  return invoke("disconnect");
}

export async function getStatus(): Promise<AppStatus> {
  return invoke("get_status");
}

// JuiceFS
export async function mountJuicefs(): Promise<string> {
  return invoke("mount_juicefs");
}

export async function unmountJuicefs(): Promise<void> {
  return invoke("unmount_juicefs");
}

// Houdini
export async function installHda(): Promise<string> {
  return invoke("install_hda");
}

export async function getHoudiniInfo(): Promise<HoudiniInfo> {
  return invoke("get_houdini_info");
}

// JuiceFS Gateway (S3-compatible, no FUSE needed)
export async function startGateway(): Promise<string> {
  return invoke<string>('start_gateway');
}

export async function stopGateway(): Promise<string> {
  return invoke<string>('stop_gateway');
}

export async function gatewayStatus(): Promise<boolean> {
  return invoke<boolean>('gateway_status');
}

// File Provider Extension (macOS)
export async function registerFileProviderDomain(): Promise<string> {
  return invoke<string>('register_fileprovider_domain');
}

export async function removeFileProviderDomain(): Promise<string> {
  return invoke<string>('remove_fileprovider_domain');
}
