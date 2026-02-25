import { useState, useEffect, useCallback } from "react";
import ConnectForm from "./components/ConnectForm";
import StatusPanel from "./components/StatusPanel";
import Settings from "./components/Settings";
import SetupScreen from "./components/SetupScreen";
import {
  getStatus,
  ensureDependencies,
  type AppStatus,
  type DependencyStatus,
} from "./lib/tauri";

type View = "setup" | "connect" | "status" | "settings";

export default function App() {
  const [view, setView] = useState<View>("setup");
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [deps, setDeps] = useState<DependencyStatus | null>(null);

  const checkDeps = useCallback(async () => {
    try {
      const depStatus = await ensureDependencies();
      setDeps(depStatus);
      return depStatus;
    } catch {
      return null;
    }
  }, []);

  const checkStatus = useCallback(async () => {
    try {
      const currentStatus = await getStatus();
      setStatus(currentStatus);
      if (currentStatus.connected) {
        setView("status");
        return;
      }
    } catch {
      // Backend not ready yet
    }

    // Not connected -- check deps
    const depStatus = await checkDeps();
    if (depStatus?.all_ready) {
      setView("connect");
    } else {
      setView("setup");
    }
  }, [checkDeps]);

  useEffect(() => {
    checkStatus();
  }, [checkStatus]);

  // Register tray disconnect handler
  useEffect(() => {
    (window as unknown as Record<string, () => void>).__trayDisconnect =
      () => {
        setStatus(null);
        setView("connect");
      };
    return () => {
      delete (window as unknown as Record<string, unknown>).__trayDisconnect;
    };
  }, []);

  const handleDepsReady = () => {
    setView("connect");
  };

  const handleConnected = (newStatus: AppStatus) => {
    setStatus(newStatus);
    setView("status");
  };

  const handleDisconnected = () => {
    setStatus(null);
    setView("connect");
  };

  const handleStatusChange = (newStatus: AppStatus) => {
    setStatus(newStatus);
  };

  return (
    <div className="min-h-screen bg-surface-950">
      {/* Navigation bar for connected state */}
      {(view === "status" || view === "settings") && (
        <nav className="sticky top-0 z-10 flex items-center justify-between border-b border-surface-800 bg-surface-950/80 px-6 py-3 backdrop-blur-sm">
          <div className="flex items-center gap-2">
            <button
              onClick={() => setView("status")}
              className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                view === "status"
                  ? "bg-surface-800 text-white"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              Status
            </button>
            <button
              onClick={() => setView("settings")}
              className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                view === "settings"
                  ? "bg-surface-800 text-white"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              Settings
            </button>
          </div>
        </nav>
      )}

      {/* Views */}
      {view === "setup" && (
        <SetupScreen deps={deps} onRecheck={checkDeps} onReady={handleDepsReady} />
      )}
      {view === "connect" && <ConnectForm onConnected={handleConnected} />}
      {view === "status" && status && (
        <StatusPanel
          status={status}
          onDisconnected={handleDisconnected}
          onStatusChange={handleStatusChange}
        />
      )}
      {view === "settings" && (
        <Settings onBack={() => setView("status")} />
      )}
    </div>
  );
}
