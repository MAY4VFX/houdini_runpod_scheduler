import { useState, useEffect } from "react";
import { connect, ensureDependencies, type AppStatus, type DependencyStatus } from "../lib/tauri";

interface ConnectFormProps {
  onConnected: (status: AppStatus) => void;
}

export default function ConnectForm({ onConnected }: ConnectFormProps) {
  const [apiKey, setApiKey] = useState("");
  const [apiUrl, setApiUrl] = useState("https://db.ai-vfx.com");
  const [mountPath, setMountPath] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingMessage, setLoadingMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [deps, setDeps] = useState<DependencyStatus | null>(null);

  useEffect(() => {
    ensureDependencies()
      .then(setDeps)
      .catch(() => {});
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKey.trim()) {
      setError("API key is required");
      return;
    }

    setLoading(true);
    setError(null);
    setLoadingMessage("Connecting...");

    try {
      // The connect command handles the full flow:
      // 1. Check FUSE -> error if missing
      // 2. Auto-download JuiceFS if missing
      // 3. Auth API call
      // 4. Auto-mount JuiceFS
      // 5. Check Houdini
      setLoadingMessage("Authenticating...");
      const status = await connect(apiKey.trim(), apiUrl.trim(), mountPath.trim() || undefined);
      if (status.mounted) {
        setLoadingMessage("Connected and mounted!");
      }
      onConnected(status);
    } catch (err) {
      const errMsg = typeof err === "string" ? err : "Connection failed";
      // Provide more helpful messages for FUSE-related errors
      if (errMsg.includes("FUSE")) {
        setError(errMsg);
      } else {
        setError(errMsg);
      }
    } finally {
      setLoading(false);
      setLoadingMessage(null);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-8">
      <div className="w-full max-w-md">
        <div className="mb-8 text-center">
          <div className="mb-4 flex items-center justify-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-indigo-600/20">
              <svg
                className="h-8 w-8 text-indigo-400"
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
          </div>
          <h1 className="text-2xl font-bold text-white">RunPodFarm</h1>
          <p className="mt-2 text-sm text-gray-400">
            Connect to your render farm
          </p>
        </div>

        <form onSubmit={handleSubmit} className="card space-y-5">
          {/* Dependency status mini-bar */}
          {deps && (
            <div className="flex items-center gap-3 rounded-lg bg-surface-800/50 px-3 py-2">
              <div className="flex items-center gap-2 text-xs">
                <span
                  className={`inline-block h-1.5 w-1.5 rounded-full ${deps.fuse_installed ? "bg-emerald-400" : "bg-red-400"}`}
                />
                <span className="text-gray-500">FUSE</span>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <span
                  className={`inline-block h-1.5 w-1.5 rounded-full ${deps.juicefs_installed ? "bg-emerald-400" : "bg-amber-400"}`}
                />
                <span className="text-gray-500">JuiceFS</span>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <span
                  className={`inline-block h-1.5 w-1.5 rounded-full ${deps.houdini_found ? "bg-emerald-400" : "bg-gray-600"}`}
                />
                <span className="text-gray-500">Houdini</span>
              </div>
            </div>
          )}

          <div>
            <label
              htmlFor="apiKey"
              className="mb-1.5 block text-sm font-medium text-gray-300"
            >
              API Key
            </label>
            <input
              id="apiKey"
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="Enter your API key"
              className="input-field"
              disabled={loading}
              autoFocus
            />
          </div>

          <div>
            <button
              type="button"
              onClick={() => setShowAdvanced(!showAdvanced)}
              className="flex items-center gap-1 text-xs text-gray-500 transition-colors hover:text-gray-300"
            >
              <svg
                className={`h-3 w-3 transition-transform ${showAdvanced ? "rotate-90" : ""}`}
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={2}
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="m8.25 4.5 7.5 7.5-7.5 7.5"
                />
              </svg>
              Advanced
            </button>

            {showAdvanced && (
              <div className="mt-3 space-y-3">
                <div>
                  <label
                    htmlFor="apiUrl"
                    className="mb-1.5 block text-sm font-medium text-gray-300"
                  >
                    API URL
                  </label>
                  <input
                    id="apiUrl"
                    type="url"
                    value={apiUrl}
                    onChange={(e) => setApiUrl(e.target.value)}
                    placeholder="https://db.ai-vfx.com"
                    className="input-field"
                    disabled={loading}
                  />
                </div>
                <div>
                  <label
                    htmlFor="mountPath"
                    className="mb-1.5 block text-sm font-medium text-gray-300"
                  >
                    Mount Path
                  </label>
                  <input
                    id="mountPath"
                    type="text"
                    value={mountPath}
                    onChange={(e) => setMountPath(e.target.value)}
                    placeholder="~/RunPodFarm (default)"
                    className="input-field"
                    disabled={loading}
                  />
                  <p className="mt-1 text-xs text-gray-500">
                    Local directory where JuiceFS will be mounted
                  </p>
                </div>
              </div>
            )}
          </div>

          {error && (
            <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-sm text-red-400">
              {error}
            </div>
          )}

          <button type="submit" className="btn-primary w-full" disabled={loading}>
            {loading ? (
              <span className="flex items-center justify-center gap-2">
                <svg
                  className="h-4 w-4 animate-spin"
                  viewBox="0 0 24 24"
                  fill="none"
                >
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                  />
                </svg>
                {loadingMessage || "Connecting..."}
              </span>
            ) : (
              "Connect"
            )}
          </button>
        </form>

        <p className="mt-6 text-center text-xs text-gray-600">
          Get your API key from the RunPodFarm dashboard
        </p>
      </div>
    </div>
  );
}
