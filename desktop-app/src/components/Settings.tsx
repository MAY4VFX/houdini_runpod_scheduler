import { useState, useEffect } from "react";
import { getHoudiniInfo, type HoudiniInfo } from "../lib/tauri";

interface SettingsProps {
  onBack: () => void;
}

export default function Settings({ onBack }: SettingsProps) {
  const [houdiniInfo, setHoudiniInfo] = useState<HoudiniInfo | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchInfo = async () => {
      try {
        const info = await getHoudiniInfo();
        setHoudiniInfo(info);
      } catch {
        // Silently fail -- we'll show "unknown" in the UI
      } finally {
        setLoading(false);
      }
    };
    fetchInfo();
  }, []);

  return (
    <div className="flex min-h-screen flex-col p-8">
      <div className="mx-auto w-full max-w-lg">
        {/* Header */}
        <div className="mb-6 flex items-center gap-3">
          <button
            onClick={onBack}
            className="flex h-8 w-8 items-center justify-center rounded-lg transition-colors hover:bg-surface-800"
          >
            <svg
              className="h-5 w-5 text-gray-400"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.5}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M15.75 19.5 8.25 12l7.5-7.5"
              />
            </svg>
          </button>
          <h1 className="text-lg font-semibold text-white">Settings</h1>
        </div>

        {/* Houdini Info */}
        <div className="card mb-4">
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-400">
            Houdini Installations
          </h2>
          {loading ? (
            <p className="text-sm text-gray-500">Scanning...</p>
          ) : houdiniInfo && houdiniInfo.installations.length > 0 ? (
            <ul className="space-y-2">
              {houdiniInfo.installations.map((path, i) => (
                <li
                  key={i}
                  className="flex items-center gap-2 rounded-lg bg-surface-800 px-3 py-2 text-sm"
                >
                  <svg
                    className="h-4 w-4 flex-shrink-0 text-emerald-400"
                    fill="none"
                    viewBox="0 0 24 24"
                    strokeWidth={1.5}
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="m4.5 12.75 6 6 9-13.5"
                    />
                  </svg>
                  <span className="truncate text-gray-300">{path}</span>
                  {path === houdiniInfo.primary && (
                    <span className="ml-auto flex-shrink-0 rounded bg-indigo-900/50 px-1.5 py-0.5 text-xs text-indigo-300">
                      primary
                    </span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-gray-500">
              No Houdini installations found
            </p>
          )}
        </div>

        {/* About */}
        <div className="card">
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-400">
            About
          </h2>
          <dl className="space-y-3">
            <div className="flex justify-between">
              <dt className="text-sm text-gray-400">Version</dt>
              <dd className="text-sm text-gray-200">0.1.0</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-sm text-gray-400">Platform</dt>
              <dd className="text-sm text-gray-200">
                {navigator.platform || "Unknown"}
              </dd>
            </div>
          </dl>
        </div>
      </div>
    </div>
  );
}
