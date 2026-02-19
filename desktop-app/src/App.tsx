import { useState, useEffect, useCallback } from "react";
import ConnectForm from "./components/ConnectForm";
import StatusPanel from "./components/StatusPanel";
import Settings from "./components/Settings";
import { getStatus, type AppStatus } from "./lib/tauri";

type View = "connect" | "status" | "settings";

export default function App() {
  const [view, setView] = useState<View>("connect");
  const [status, setStatus] = useState<AppStatus | null>(null);

  const checkStatus = useCallback(async () => {
    try {
      const currentStatus = await getStatus();
      setStatus(currentStatus);
      if (currentStatus.connected) {
        setView("status");
      }
    } catch {
      // Backend not ready yet or error -- stay on connect view
    }
  }, []);

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
      {view !== "connect" && (
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
