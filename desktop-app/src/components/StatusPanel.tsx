import { useState, useEffect } from "react";
import {
  disconnect,
  mountJuicefs,
  unmountJuicefs,
  installHda,
  ensureDependencies,
  type AppStatus,
  type DependencyStatus,
} from "../lib/tauri";

interface StatusPanelProps {
  status: AppStatus;
  onDisconnected: () => void;
  onStatusChange: (status: AppStatus) => void;
}

function StatusDot({ active }: { active: boolean }) {
  return (
    <span
      className={`inline-block h-2.5 w-2.5 rounded-full ${
        active ? "bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.5)]" : "bg-gray-600"
      }`}
    />
  );
}

function StatusRow({
  label,
  value,
  active,
  action,
}: {
  label: string;
  value: string;
  active: boolean;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between py-3">
      <div className="flex items-center gap-3">
        <StatusDot active={active} />
        <div>
          <p className="text-sm font-medium text-gray-200">{label}</p>
          <p className="text-xs text-gray-500">{value}</p>
        </div>
      </div>
      {action && <div>{action}</div>}
    </div>
  );
}

export default function StatusPanel({
  status,
  onDisconnected,
  onStatusChange,
}: StatusPanelProps) {
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [deps, setDeps] = useState<DependencyStatus | null>(null);

  useEffect(() => {
    ensureDependencies()
      .then(setDeps)
      .catch(() => {});
  }, []);

  const handleDisconnect = async () => {
    setLoading("disconnect");
    setError(null);
    try {
      await disconnect();
      onDisconnected();
    } catch (err) {
      setError(typeof err === "string" ? err : "Disconnect failed");
    } finally {
      setLoading(null);
    }
  };

  const handleMount = async () => {
    setLoading("mount");
    setError(null);
    setMessage(null);
    try {
      const mountPath = await mountJuicefs();
      onStatusChange({
        ...status,
        mounted: true,
        mount_path: mountPath,
      });
      setMessage(`Mounted at ${mountPath}`);
    } catch (err) {
      setError(typeof err === "string" ? err : "Mount failed");
    } finally {
      setLoading(null);
    }
  };

  const handleUnmount = async () => {
    setLoading("unmount");
    setError(null);
    setMessage(null);
    try {
      await unmountJuicefs();
      onStatusChange({
        ...status,
        mounted: false,
        mount_path: null,
      });
      setMessage("Unmounted successfully");
    } catch (err) {
      setError(typeof err === "string" ? err : "Unmount failed");
    } finally {
      setLoading(null);
    }
  };

  const handleInstallHda = async () => {
    setLoading("hda");
    setError(null);
    setMessage(null);
    try {
      const result = await installHda();
      onStatusChange({
        ...status,
        hda_installed: true,
      });
      setMessage(result);
    } catch (err) {
      setError(typeof err === "string" ? err : "HDA install failed");
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="flex min-h-screen flex-col p-8">
      <div className="mx-auto w-full max-w-lg">
        {/* Header */}
        <div className="mb-6 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-600/20">
              <svg
                className="h-5 w-5 text-indigo-400"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={1.5}
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M5.25 14.25h13.5m-13.5 0a3 3 0 0 1-3-3m3 3a3 3 0 1 0 0 6h13.5a3 3 0 1 0 0-6m-16.5-3a3 3 0 0 1 3-3h13.5a3 3 0 0 1 3 3m-19.5 0a4.5 4.5 0 0 1 .9-2.7L5.737 5.1a3.375 3.375 0 0 1 2.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 0 1 .9 2.7m0 0a3 3 0 0 1-3 3m0 3h.008v.008h-.008v-.008Zm0-6h.008v.008h-.008v-.008Zm-3 6h.008v.008h-.008v-.008Zm0-6h.008v.008h-.008v-.008Z"
                />
              </svg>
            </div>
            <div>
              <h1 className="text-lg font-semibold text-white">RunPodFarm</h1>
              <p className="text-xs text-gray-500">Desktop Client</p>
            </div>
          </div>
          <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-900/30 px-3 py-1 text-xs font-medium text-emerald-400">
            <StatusDot active={true} />
            Connected
          </span>
        </div>

        {/* Dependencies info */}
        {deps && (
          <div className="card mb-4">
            <h2 className="mb-1 text-sm font-semibold uppercase tracking-wider text-gray-400">
              Dependencies
            </h2>
            <div className="divide-y divide-surface-800">
              <StatusRow
                label="FUSE Driver"
                value={deps.fuse_installed ? "Installed" : "Not installed"}
                active={deps.fuse_installed}
              />
              <StatusRow
                label="JuiceFS Client"
                value={
                  deps.juicefs_installed
                    ? deps.juicefs_path || "Installed"
                    : "Not installed"
                }
                active={deps.juicefs_installed}
              />
            </div>
          </div>
        )}

        {/* Status Cards */}
        <div className="card mb-4">
          <h2 className="mb-1 text-sm font-semibold uppercase tracking-wider text-gray-400">
            Status
          </h2>
          <div className="divide-y divide-surface-800">
            <StatusRow
              label="Connection"
              value={
                status.project_id
                  ? `Project: ${status.project_id}`
                  : "Connected"
              }
              active={status.connected}
            />
            <StatusRow
              label="JuiceFS Mount"
              value={
                status.mounted
                  ? status.mount_path || "Mounted"
                  : "Not mounted"
              }
              active={status.mounted}
              action={
                status.mounted ? (
                  <button
                    className="btn-secondary text-xs"
                    onClick={handleUnmount}
                    disabled={loading === "unmount"}
                  >
                    {loading === "unmount" ? "..." : "Unmount"}
                  </button>
                ) : undefined
              }
            />
            <StatusRow
              label="Houdini"
              value={status.houdini_found ? "Detected" : "Not found"}
              active={status.houdini_found}
            />
            <StatusRow
              label="RunPodFarm HDA"
              value={status.hda_installed ? "Installed" : "Not installed"}
              active={status.hda_installed}
              action={
                !status.hda_installed && status.houdini_found ? (
                  <button
                    className="btn-primary text-xs"
                    onClick={handleInstallHda}
                    disabled={loading === "hda"}
                  >
                    {loading === "hda" ? "..." : "Install"}
                  </button>
                ) : undefined
              }
            />
          </div>
        </div>

        {/* Messages */}
        {error && (
          <div className="mb-4 rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-sm text-red-400">
            {error}
          </div>
        )}
        {message && (
          <div className="mb-4 rounded-lg border border-emerald-800/50 bg-emerald-900/20 px-4 py-3 text-sm text-emerald-400">
            {message}
          </div>
        )}

        {/* Disconnect */}
        <button
          className="btn-danger w-full"
          onClick={handleDisconnect}
          disabled={loading === "disconnect"}
        >
          {loading === "disconnect" ? "Disconnecting..." : "Disconnect"}
        </button>
      </div>
    </div>
  );
}
