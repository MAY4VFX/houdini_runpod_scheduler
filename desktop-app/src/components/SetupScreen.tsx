import { useState, useEffect, useCallback } from "react";
import {
  downloadJuicefs,
  ensureDependencies,
  type DependencyStatus,
} from "../lib/tauri";

interface SetupScreenProps {
  deps: DependencyStatus | null;
  onRecheck: () => Promise<DependencyStatus | null>;
  onReady: () => void;
}

type SetupPhase = "checking" | "downloading_juicefs" | "needs_fuse" | "ready";

function Spinner() {
  return (
    <svg
      className="h-5 w-5 animate-spin text-indigo-400"
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
  );
}

function CheckIcon() {
  return (
    <svg
      className="h-5 w-5 text-emerald-400"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={2}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="m4.5 12.75 6 6 9-13.5"
      />
    </svg>
  );
}

function CrossIcon() {
  return (
    <svg
      className="h-5 w-5 text-red-400"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={2}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M6 18 18 6M6 6l12 12"
      />
    </svg>
  );
}

function WarningIcon() {
  return (
    <svg
      className="h-5 w-5 text-amber-400"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={2}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"
      />
    </svg>
  );
}

export default function SetupScreen({
  deps: initialDeps,
  onRecheck,
  onReady,
}: SetupScreenProps) {
  const [phase, setPhase] = useState<SetupPhase>("checking");
  const [deps, setDeps] = useState<DependencyStatus | null>(initialDeps);
  const [error, setError] = useState<string | null>(null);
  const [downloadProgress, setDownloadProgress] = useState<string>("");

  const runSetup = useCallback(async () => {
    setPhase("checking");
    setError(null);

    // Step 1: Check current state
    let currentDeps: DependencyStatus | null;
    try {
      currentDeps = await ensureDependencies();
      setDeps(currentDeps);
    } catch {
      setError("Failed to check dependencies");
      return;
    }

    if (!currentDeps) return;

    // Step 2: If JuiceFS missing, download it automatically
    if (!currentDeps.juicefs_installed) {
      setPhase("downloading_juicefs");
      setDownloadProgress("Downloading JuiceFS v1.2.0...");
      try {
        const path = await downloadJuicefs();
        setDownloadProgress(`Installed to ${path}`);
        // Recheck
        currentDeps = await ensureDependencies();
        setDeps(currentDeps);
      } catch (err) {
        setError(
          `Failed to download JuiceFS: ${typeof err === "string" ? err : "Unknown error"}`
        );
        return;
      }
    }

    // Step 3: Check FUSE
    if (!currentDeps || !currentDeps.fuse_installed) {
      setPhase("needs_fuse");
      return;
    }

    // All good
    setPhase("ready");
  }, []);

  useEffect(() => {
    runSetup();
  }, [runSetup]);

  const handleRecheckFuse = async () => {
    setError(null);
    const newDeps = await onRecheck();
    if (newDeps) {
      setDeps(newDeps);
      if (newDeps.fuse_installed) {
        if (newDeps.all_ready) {
          setPhase("ready");
        } else {
          // Re-run full setup
          runSetup();
        }
      }
    }
  };

  // Auto-proceed when ready
  useEffect(() => {
    if (phase === "ready") {
      const timer = setTimeout(() => onReady(), 800);
      return () => clearTimeout(timer);
    }
  }, [phase, onReady]);

  return (
    <div className="flex min-h-screen items-center justify-center p-8">
      <div className="w-full max-w-md">
        {/* Header */}
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
                  d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z"
                />
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z"
                />
              </svg>
            </div>
          </div>
          <h1 className="text-2xl font-bold text-white">RunPodFarm Setup</h1>
          <p className="mt-2 text-sm text-gray-400">
            Checking required dependencies...
          </p>
        </div>

        {/* Dependency checklist */}
        <div className="card space-y-1">
          {/* FUSE driver */}
          <div className="flex items-center gap-3 rounded-lg px-3 py-3">
            <div className="flex-shrink-0">
              {phase === "checking" ? (
                <Spinner />
              ) : deps?.fuse_installed ? (
                <CheckIcon />
              ) : (
                <CrossIcon />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-gray-200">FUSE Driver</p>
              <p className="text-xs text-gray-500">
                {phase === "checking"
                  ? "Checking..."
                  : deps?.fuse_installed
                    ? "Installed"
                    : "Not installed -- required for JuiceFS"}
              </p>
            </div>
          </div>

          {/* JuiceFS */}
          <div className="flex items-center gap-3 rounded-lg px-3 py-3">
            <div className="flex-shrink-0">
              {phase === "checking" ? (
                <Spinner />
              ) : phase === "downloading_juicefs" ? (
                <Spinner />
              ) : deps?.juicefs_installed ? (
                <CheckIcon />
              ) : (
                <CrossIcon />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-gray-200">
                JuiceFS Client
              </p>
              <p className="text-xs text-gray-500">
                {phase === "checking"
                  ? "Checking..."
                  : phase === "downloading_juicefs"
                    ? downloadProgress
                    : deps?.juicefs_installed
                      ? deps.juicefs_path
                        ? `Installed: ${deps.juicefs_path}`
                        : "Installed"
                      : "Not installed -- will download automatically"}
              </p>
            </div>
          </div>

          {/* Houdini (optional) */}
          <div className="flex items-center gap-3 rounded-lg px-3 py-3">
            <div className="flex-shrink-0">
              {phase === "checking" ? (
                <Spinner />
              ) : deps?.houdini_found ? (
                <CheckIcon />
              ) : (
                <WarningIcon />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-gray-200">
                SideFX Houdini
              </p>
              <p className="text-xs text-gray-500">
                {phase === "checking"
                  ? "Checking..."
                  : deps?.houdini_found
                    ? "Detected"
                    : "Not found (optional -- can be installed later)"}
              </p>
            </div>
          </div>
        </div>

        {/* Error */}
        {error && (
          <div className="mt-4 rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-sm text-red-400">
            {error}
          </div>
        )}

        {/* FUSE install instructions */}
        {phase === "needs_fuse" && deps && !deps.fuse_installed && (
          <div className="mt-4 space-y-3">
            <div className="rounded-lg border border-amber-800/50 bg-amber-900/20 px-4 py-4">
              <p className="mb-2 text-sm font-medium text-amber-300">
                FUSE driver required
              </p>
              <p className="mb-3 text-xs text-amber-400/80">
                {deps.fuse_install_instructions}
              </p>
              {deps.fuse_install_url && (
                <a
                  href={deps.fuse_install_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 rounded-lg bg-amber-600/20 px-3 py-1.5 text-xs font-medium text-amber-300 transition-colors hover:bg-amber-600/30"
                >
                  <svg
                    className="h-3.5 w-3.5"
                    fill="none"
                    viewBox="0 0 24 24"
                    strokeWidth={2}
                    stroke="currentColor"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M13.5 6H5.25A2.25 2.25 0 0 0 3 8.25v10.5A2.25 2.25 0 0 0 5.25 21h10.5A2.25 2.25 0 0 0 18 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25"
                    />
                  </svg>
                  Download FUSE Driver
                </a>
              )}
            </div>

            <button
              onClick={handleRecheckFuse}
              className="btn-primary w-full"
            >
              I've installed FUSE -- Check again
            </button>
          </div>
        )}

        {/* Ready state */}
        {phase === "ready" && (
          <div className="mt-4 rounded-lg border border-emerald-800/50 bg-emerald-900/20 px-4 py-3 text-center text-sm text-emerald-400">
            All dependencies ready. Continuing...
          </div>
        )}

        {/* Retry button on error */}
        {error && (
          <button
            onClick={runSetup}
            className="btn-secondary mt-4 w-full"
          >
            Retry
          </button>
        )}
      </div>
    </div>
  );
}
