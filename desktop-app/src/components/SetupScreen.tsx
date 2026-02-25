import { useState, useEffect, useCallback } from "react";
import {
  downloadJuicefs,
  installFuse,
  ensureDependencies,
  type DependencyStatus,
} from "../lib/tauri";

interface SetupScreenProps {
  deps: DependencyStatus | null;
  onRecheck: () => Promise<DependencyStatus | null>;
  onReady: () => void;
}

type SetupPhase =
  | "checking"
  | "downloading_juicefs"
  | "installing_fuse"
  | "fuse_pending"
  | "ready";

function Spinner() {
  return (
    <svg className="h-5 w-5 animate-spin text-indigo-400" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg className="h-5 w-5 text-emerald-400" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
    </svg>
  );
}

function CrossIcon() {
  return (
    <svg className="h-5 w-5 text-red-400" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18 18 6M6 6l12 12" />
    </svg>
  );
}

function WarningIcon() {
  return (
    <svg className="h-5 w-5 text-amber-400" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
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
  const [statusMsg, setStatusMsg] = useState<string>("");
  const [fuseError, setFuseError] = useState<string>("");
  const [rechecking, setRechecking] = useState(false);

  const runSetup = useCallback(async () => {
    setPhase("checking");
    setError(null);
    setFuseError("");
    setStatusMsg("");

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

    // Step 2: Auto-download JuiceFS if missing
    if (!currentDeps.juicefs_installed) {
      setPhase("downloading_juicefs");
      setStatusMsg("Downloading JuiceFS v1.2.0...");
      try {
        const path = await downloadJuicefs();
        setStatusMsg(`Installed to ${path}`);
        currentDeps = await ensureDependencies();
        setDeps(currentDeps);
      } catch (err) {
        setError(`Failed to download JuiceFS: ${typeof err === "string" ? err : "Unknown error"}`);
        return;
      }
    }

    // Step 3: Auto-install FUSE if missing
    if (!currentDeps || !currentDeps.fuse_installed) {
      setPhase("installing_fuse");
      setStatusMsg("Installing macFUSE (you may be prompted for your password)...");
      try {
        const msg = await installFuse();
        setStatusMsg(msg);
        // Recheck after install
        currentDeps = await ensureDependencies();
        setDeps(currentDeps);
        if (!currentDeps || !currentDeps.fuse_installed) {
          // Installed but needs kext approval / restart
          setPhase("fuse_pending");
          setFuseError(msg);
          return;
        }
      } catch (err) {
        setPhase("fuse_pending");
        setFuseError(typeof err === "string" ? err : "macFUSE installation failed.");
        return;
      }
    }

    // All good!
    setPhase("ready");
  }, []);

  useEffect(() => {
    runSetup();
  }, [runSetup]);

  // Auto-proceed when ready
  useEffect(() => {
    if (phase === "ready") {
      const timer = setTimeout(() => onReady(), 800);
      return () => clearTimeout(timer);
    }
  }, [phase, onReady]);

  const handleRecheck = async () => {
    setRechecking(true);
    setError(null);
    try {
      const newDeps = await onRecheck();
      if (newDeps) {
        setDeps(newDeps);
        if (newDeps.all_ready) {
          setPhase("ready");
        } else if (!newDeps.fuse_installed) {
          setError("FUSE still not detected.");
        } else {
          // FUSE now OK, re-run full setup
          runSetup();
        }
      }
    } catch (err) {
      setError(`Check failed: ${typeof err === "string" ? err : "Unknown error"}`);
    } finally {
      setRechecking(false);
    }
  };

  const handleRetryFuse = async () => {
    setPhase("installing_fuse");
    setFuseError("");
    setError(null);
    setStatusMsg("Installing macFUSE...");
    try {
      const msg = await installFuse();
      setStatusMsg(msg);
      const newDeps = await ensureDependencies();
      setDeps(newDeps);
      if (newDeps?.fuse_installed) {
        if (newDeps.all_ready) {
          setPhase("ready");
        } else {
          runSetup();
        }
      } else {
        setPhase("fuse_pending");
        setFuseError(msg);
      }
    } catch (err) {
      setPhase("fuse_pending");
      setFuseError(typeof err === "string" ? err : "Installation failed.");
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-8">
      <div className="w-full max-w-md">
        {/* Header */}
        <div className="mb-8 text-center">
          <div className="mb-4 flex items-center justify-center">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-indigo-600/20">
              <svg className="h-8 w-8 text-indigo-400" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 0 1 1.37.49l1.296 2.247a1.125 1.125 0 0 1-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 0 1 0 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 0 1-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 0 1-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 0 1-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 0 1-1.369-.49l-1.297-2.247a1.125 1.125 0 0 1 .26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 0 1 0-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 0 1-.26-1.43l1.297-2.247a1.125 1.125 0 0 1 1.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28Z" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z" />
              </svg>
            </div>
          </div>
          <h1 className="text-2xl font-bold text-white">RunPodFarm Setup</h1>
          <p className="mt-2 text-sm text-gray-400">
            {phase === "ready"
              ? "All set!"
              : phase === "installing_fuse"
                ? "Installing macFUSE..."
                : phase === "downloading_juicefs"
                  ? "Downloading JuiceFS..."
                  : "Checking dependencies..."}
          </p>
        </div>

        {/* Dependency checklist */}
        <div className="card space-y-1">
          {/* FUSE driver */}
          <div className="flex items-center gap-3 rounded-lg px-3 py-3">
            <div className="flex-shrink-0">
              {phase === "checking" || phase === "installing_fuse" ? (
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
                  : phase === "installing_fuse"
                    ? statusMsg || "Installing..."
                    : deps?.fuse_installed
                      ? "Installed"
                      : "Not installed"}
              </p>
            </div>
          </div>

          {/* JuiceFS */}
          <div className="flex items-center gap-3 rounded-lg px-3 py-3">
            <div className="flex-shrink-0">
              {phase === "checking" || phase === "downloading_juicefs" ? (
                <Spinner />
              ) : deps?.juicefs_installed ? (
                <CheckIcon />
              ) : (
                <CrossIcon />
              )}
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-gray-200">JuiceFS Client</p>
              <p className="text-xs text-gray-500">
                {phase === "checking"
                  ? "Checking..."
                  : phase === "downloading_juicefs"
                    ? statusMsg
                    : deps?.juicefs_installed
                      ? deps.juicefs_path
                        ? `Installed: ${deps.juicefs_path}`
                        : "Installed"
                      : "Not installed"}
              </p>
            </div>
          </div>

          {/* Houdini */}
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
              <p className="text-sm font-medium text-gray-200">SideFX Houdini</p>
              <p className="text-xs text-gray-500">
                {phase === "checking"
                  ? "Checking..."
                  : deps?.houdini_found
                    ? "Detected"
                    : "Not found (optional)"}
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

        {/* FUSE pending — installed but needs approval or retry */}
        {phase === "fuse_pending" && (
          <div className="mt-4 space-y-3">
            <div className="rounded-lg border border-amber-800/50 bg-amber-900/20 px-4 py-4">
              <p className="mb-2 text-sm font-medium text-amber-300">
                macFUSE needs attention
              </p>
              <p className="text-xs text-amber-400/80">
                {fuseError || "macFUSE was installed but may need system extension approval. Go to System Settings > Privacy & Security and approve macFUSE, then click Check again."}
              </p>
            </div>

            <div className="flex gap-2">
              <button
                onClick={handleRetryFuse}
                className="btn-secondary flex-1"
              >
                Retry Install
              </button>
              <button
                onClick={handleRecheck}
                disabled={rechecking}
                className="btn-primary flex-1 disabled:opacity-50"
              >
                {rechecking ? "Checking..." : "Check again"}
              </button>
            </div>
          </div>
        )}

        {/* Ready state */}
        {phase === "ready" && (
          <div className="mt-4 rounded-lg border border-emerald-800/50 bg-emerald-900/20 px-4 py-3 text-center text-sm text-emerald-400">
            All dependencies ready. Continuing...
          </div>
        )}

        {/* Retry button on error */}
        {error && phase !== "fuse_pending" && (
          <button onClick={runSetup} className="btn-secondary mt-4 w-full">
            Retry
          </button>
        )}
      </div>
    </div>
  );
}
