import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  type AnalysisStatus,
  type GameList,
  type Settings,
} from "./api";
import { GameView } from "./GameView";
import { monthAndYear, relativeTime, shortDate, timeControl } from "./format";

/** Module scope, not a ref: returning from a game view remounts the list, and
 *  the auto-sync on open (PRD 4.2) should not fire again on every navigation. */
let autoSyncedThisSession = false;

export default function App() {
  const gameId = useHashRoute();

  if (gameId !== null) {
    return <GameView gameId={gameId} onBack={() => navigate(null)} />;
  }
  return <GameListScreen />;
}

function navigate(gameId: number | null) {
  window.location.hash = gameId === null ? "" : `#/game/${gameId}`;
}

/** A hash route rather than a router: two screens, and browser back works. */
function useHashRoute(): number | null {
  const parse = () => {
    const match = /^#\/game\/(\d+)$/.exec(window.location.hash);
    return match ? Number(match[1]) : null;
  };

  const [gameId, setGameId] = useState<number | null>(parse);

  useEffect(() => {
    const onChange = () => setGameId(parse());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return gameId;
}

function GameListScreen() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [games, setGames] = useState<GameList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [analysis, setAnalysis] = useState<AnalysisStatus | null>(null);

  const loadGames = useCallback(async () => {
    const [list, status] = await Promise.all([api.games(), api.analysisStatus()]);
    setGames(list);
    setAnalysis(status);
  }, []);

  const runSync = useCallback(async () => {
    setSyncing(true);
    setError(null);
    try {
      await api.sync();
      setSettings(await api.settings());
      await loadGames();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSyncing(false);
    }
  }, [loadGames]);

  useEffect(() => {
    (async () => {
      try {
        const loaded = await api.settings();
        setSettings(loaded);
        await loadGames();
        if (loaded.chesscom_enabled && !autoSyncedThisSession) {
          autoSyncedThisSession = true;
          await runSync();
        }
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      }
    })();
  }, [loadGames, runSync]);

  const outstanding =
    games?.games.some(
      (game) =>
        game.analysis_status === "pending" || game.analysis_status === "in_progress",
    ) ?? false;

  // Analysis runs behind the list, so the list has to come back and look.
  useEffect(() => {
    if (!outstanding) return;
    const timer = setInterval(() => {
      loadGames().catch(() => {
        /* transient; the next tick retries */
      });
    }, 2000);
    return () => clearInterval(timer);
  }, [outstanding, loadGames]);

  if (!settings) {
    return (
      <main>
        <h1>Chess Analysis</h1>
        {error ? (
          <Banner message={error} onDismiss={() => setError(null)} />
        ) : (
          <p className="muted">Loading…</p>
        )}
      </main>
    );
  }

  return (
    <main>
      <h1>Chess Analysis</h1>

      {error && <Banner message={error} onDismiss={() => setError(null)} />}

      <SettingsPanel
        settings={settings}
        onSaved={async (saved) => {
          setSettings(saved);
          setError(null);
          if (saved.chesscom_enabled) await runSync();
        }}
        onError={setError}
      />

      {settings.chesscom_enabled && (
        <section>
          <div className="toolbar">
            <button onClick={runSync} disabled={syncing}>
              {syncing ? "Syncing…" : "Sync now"}
            </button>
            <span className="muted">
              Last synced: {relativeTime(settings.chesscom_last_synced_at)}
            </span>
          </div>

          <AnalysisProgress status={analysis} />

          <GameTable
            games={games}
            onOpen={navigate}
            onRetry={async (id) => {
              await api.analyseGame(id, true);
              await loadGames();
            }}
          />

          {games && games.total > 0 && (
            <p className="muted">
              Showing {games.games.length} of {games.total}. History loaded back to{" "}
              {monthAndYear(games.history_back_to)}.
            </p>
          )}
        </section>
      )}
    </main>
  );
}

function Banner({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  return (
    <div className="banner" role="alert">
      <span>{message}</span>
      <button onClick={onDismiss} aria-label="Dismiss">
        ×
      </button>
    </div>
  );
}

function SettingsPanel({
  settings,
  onSaved,
  onError,
}: {
  settings: Settings;
  onSaved: (settings: Settings) => void;
  onError: (message: string) => void;
}) {
  const [enabled, setEnabled] = useState(settings.chesscom_enabled);
  const [username, setUsername] = useState(settings.chesscom_username ?? "");
  const [depth, setDepth] = useState(settings.analysis_depth);
  const [background, setBackground] = useState(settings.background_analysis);
  const [saving, setSaving] = useState(false);
  // Nothing configured yet: open on settings rather than an empty list (PRD 7).
  const [open, setOpen] = useState(!settings.chesscom_enabled);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const saved = await api.saveSettings({
        chesscom_enabled: enabled,
        chesscom_username: username.trim() || null,
        analysis_depth: depth,
        background_analysis: background,
      });
      setOpen(false);
      onSaved(saved);
    } catch (err) {
      onError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <p className="muted">
        Chess.com: <strong>{settings.chesscom_username}</strong>{" "}
        <button className="link" onClick={() => setOpen(true)}>
          change
        </button>
      </p>
    );
  }

  return (
    <form className="settings" onSubmit={save}>
      <h2>Settings</h2>
      {!settings.chesscom_enabled && <p>Add an account to get started.</p>}
      <label>
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        Chess.com
      </label>
      <label>
        Username
        <input
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          placeholder="your Chess.com username"
          autoComplete="off"
        />
      </label>
      <label>
        Engine depth
        <input
          type="number"
          min={6}
          max={30}
          value={depth}
          onChange={(event) => setDepth(Number(event.target.value))}
        />
      </label>
      <p className="muted">
        18–20 is the target. Lower is much faster; evaluations are only
        comparable between games analysed at the same depth.
      </p>
      <label>
        <input
          type="checkbox"
          checked={background}
          onChange={(event) => setBackground(event.target.checked)}
        />
        Analyse the whole archive in the background
      </label>
      <p className="muted">
        Off means only games you open get analysed. Opening a game always jumps
        the queue either way.
      </p>
      <button type="submit" disabled={saving}>
        {saving ? "Checking…" : "Save"}
      </button>
    </form>
  );
}

function AnalysisProgress({ status }: { status: AnalysisStatus | null }) {
  if (!status) return null;

  if (status.error) {
    return (
      <div className="banner" role="alert">
        Analysis is not running: {status.error}
      </div>
    );
  }

  if (status.queued === 0 && status.current_game_id === null) return null;

  const position =
    status.current_total > 0
      ? ` (position ${status.current_ply}/${status.current_total})`
      : "";

  return (
    <p className="muted">
      Analysing {status.queued + (status.current_game_id === null ? 0 : 1)} game
      {status.queued === 1 && status.current_game_id === null ? "" : "s"}
      {position}…
    </p>
  );
}

function GameTable({
  games,
  onOpen,
  onRetry,
}: {
  games: GameList | null;
  onOpen: (id: number) => void;
  onRetry: (id: number) => void;
}) {
  if (!games) return <p className="muted">Loading games…</p>;
  if (games.total === 0) {
    return <p className="muted">No games yet. Hit “Sync now”.</p>;
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Date</th>
          <th>Colour</th>
          <th>Opponent</th>
          <th>Result</th>
          <th>Time</th>
          <th>ECO</th>
          <th>Analysis</th>
        </tr>
      </thead>
      <tbody>
        {games.games.map((game) => (
          <tr key={game.id} className="clickable" onClick={() => onOpen(game.id)}>
            <td>{shortDate(game.played_at)}</td>
            <td>{game.player_color ?? "—"}</td>
            <td>{game.opponent ?? "—"}</td>
            <td className={game.result ?? undefined}>{game.result ?? "—"}</td>
            <td>{timeControl(game.time_control)}</td>
            <td>{game.eco_code ?? "—"}</td>
            <td onClick={(event) => event.stopPropagation()}>
              <AnalysisBadge status={game.analysis_status} onRetry={() => onRetry(game.id)} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function AnalysisBadge({
  status,
  onRetry,
}: {
  status: string;
  onRetry: () => void;
}) {
  switch (status) {
    case "complete":
      return <span className="badge done">analysed</span>;
    case "in_progress":
      return <span className="badge working">analysing…</span>;
    case "pending":
      return <span className="badge">queued</span>;
    case "unanalysable":
      return <span className="badge muted">no moves</span>;
    case "failed":
      return (
        <button className="link failed" onClick={onRetry}>
          failed — retry
        </button>
      );
    default:
      return <span className="badge muted">{status}</span>;
  }
}
