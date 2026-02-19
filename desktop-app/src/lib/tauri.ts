import { invoke } from "@tauri-apps/api/core";

export interface AppStatus {
  connected: boolean;
  mounted: boolean;
  project_id: string | null;
  mount_path: string | null;
  houdini_found: boolean;
  hda_installed: boolean;
}

export interface HoudiniInfo {
  installations: string[];
  primary: string | null;
}

export async function connect(
  apiKey: string,
  apiUrl: string
): Promise<AppStatus> {
  return invoke("connect", { apiKey, apiUrl });
}

export async function disconnect(): Promise<void> {
  return invoke("disconnect");
}

export async function getStatus(): Promise<AppStatus> {
  return invoke("get_status");
}

export async function mountJuicefs(): Promise<string> {
  return invoke("mount_juicefs");
}

export async function unmountJuicefs(): Promise<void> {
  return invoke("unmount_juicefs");
}

export async function installHda(): Promise<string> {
  return invoke("install_hda");
}

export async function getHoudiniInfo(): Promise<HoudiniInfo> {
  return invoke("get_houdini_info");
}
