import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  platformName,
  type AnalysisStatus,
  type GameFilters,
  type GameList,
  type Settings,
} from "./api";
import { GameView } from "./GameView";
import { ErrorCounts, accuracyPercent } from "./Summary";
import {
  compactDate,
  monthAndYear,
  relativeTime,
  shortDate,
  timeControl,
} from "./format";

/** Module scope, not a ref: returning from a game view remounts the list, and
 *  the auto-sync on open (PRD 4.2) should not fire again on every navigation. */
let autoSyncedThisSession = false;

const PAGE_SIZE = 50;
/** The API's ceiling on one request. Past it, filtering is the way further
 *  back, which is cheaper than paging through hundreds of rows anyway. */
const MAX_SHOWN = 500;

function describe(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

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
  const [filters, setFilters] = useState<GameFilters>({});
  /** How many rows are open. The window is re-requested whole rather than
   *  appended to, so a refresh cannot leave half the list stale. */
  const [shown, setShown] = useState(PAGE_SIZE);
  /** Null until the user says either way; settings then open only when there is
   *  no account yet, so a first run lands on them rather than an empty list
   *  (PRD 7). */
  const [settingsOpen, setSettingsOpen] = useState<boolean | null>(null);

  const loadGames = useCallback(async () => {
    const [list, status] = await Promise.all([
      api.games(shown, 0, filters),
      api.analysisStatus(),
    ]);
    setGames(list);
    setAnalysis(status);
  }, [shown, filters]);

  const runSync = useCallback(async () => {
    setSyncing(true);
    setError(null);
    try {
      const result = await api.sync();
      setSettings(await api.settings());
      // A platform that failed while another succeeded is reported, not
      // thrown: its games are simply missing from an otherwise fresh list, and
      // that has to be visible (PRD 4.2).
      if (result.failures.length > 0) {
        setError(
          result.failures
            .map((f) => `${platformName(f.platform)}: ${f.message}`)
            .join(" · "),
        );
      }
      // New games land at the top; whatever was paged open describes the list
      // as it was before them.
      setShown(PAGE_SIZE);
      await loadGames();
    } catch (err) {
      setError(describe(err));
    } finally {
      setSyncing(false);
    }
  }, [loadGames]);

  // Settings, and the sync-on-open, exactly once. The games load is its own
  // effect below so that changing a filter does not re-run any of this.
  useEffect(() => {
    (async () => {
      try {
        const loaded = await api.settings();
        setSettings(loaded);
        if (isConnected(loaded) && !autoSyncedThisSession) {
          autoSyncedThisSession = true;
          await runSync();
        }
      } catch (err) {
        setError(describe(err));
      }
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    loadGames().catch((err) => setError(describe(err)));
  }, [loadGames]);

  // The queue, not the rows: with background analysis off, games sit at
  // "pending" forever by design, and polling for a change that is never coming
  // would keep a request every two seconds running for the life of the tab.
  const working =
    analysis !== null &&
    !analysis.error &&
    (analysis.queued > 0 || analysis.current_game_id !== null);

  // Analysis runs behind the list, so the list has to come back and look.
  useEffect(() => {
    if (!working) return;
    const timer = setInterval(() => {
      loadGames().catch(() => {
        /* transient; the next tick retries */
      });
    }, 2000);
    return () => clearInterval(timer);
  }, [working, loadGames]);

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

  const filtered = Object.values(filters).some(Boolean);
  const more = games !== null && games.games.length < games.total;
  const connected = isConnected(settings);
  const showSettings = settingsOpen ?? !connected;

  return (
    <main>
      <div className="page-head">
        <h1>Chess Analysis</h1>
        {connected && (
          <Accounts
            settings={settings}
            onToggleSettings={() => setSettingsOpen(!showSettings)}
          />
        )}
      </div>

      {error && <Banner message={error} onDismiss={() => setError(null)} />}

      {showSettings && (
        <SettingsPanel
          settings={settings}
          onSaved={async (saved) => {
            setSettings(saved);
            setError(null);
            if (isConnected(saved)) await runSync();
          }}
          onError={setError}
          onClose={() => setSettingsOpen(false)}
        />
      )}

      {connected && (
        <section>
          {/* Sync, the filters and whatever analysis is running are one bar.
              Split across separate rows they were three thin things stacked
              above the table, each mostly empty space. */}
          <div className="toolbar panel">
            <button onClick={runSync} disabled={syncing}>
              {syncing ? "Syncing…" : "Sync now"}
            </button>

            <Filters
              filters={filters}
              onChange={(next) => {
                setShown(PAGE_SIZE);
                setFilters(next);
              }}
            />

            <div className="spacer" />
            <AnalysisProgress status={analysis} />
          </div>

          <div className="games panel">
            <GameTable
              games={games}
              filtered={filtered}
              onOpen={navigate}
              onRetry={async (id) => {
                await api.analyseGame(id, true);
                await loadGames();
              }}
            />

            {games && games.total > 0 && (
              <div className="more">
                {more && (
                  <button
                    onClick={() =>
                      setShown((count) => Math.min(count + PAGE_SIZE, MAX_SHOWN))
                    }
                    disabled={shown >= MAX_SHOWN}
                  >
                    Show {Math.min(PAGE_SIZE, games.total - games.games.length)} more
                  </button>
                )}
                <span className="muted">
                  Showing {games.games.length} of {games.total}
                  {filtered ? " matching" : ""}
                  {shown >= MAX_SHOWN && more
                    ? " — narrow with the filters to reach older games"
                    : "."}{" "}
                  History loaded back to {monthAndYear(games.history_back_to)}.
                </span>
              </div>
            )}
          </div>
        </section>
      )}
    </main>
  );
}

/** Whether there is any account to sync. Both platforms are independent and
 *  either alone is enough, so nothing may key off Chess.com specifically. */
function isConnected(settings: Settings): boolean {
  return settings.chesscom_enabled || settings.lichess_enabled;
}

/** The connected accounts and how fresh each one is. Freshness is per account,
 *  not per app: one platform can fail or be connected later, and a single
 *  "synced 2 minutes ago" would then vouch for games never fetched. */
function Accounts({
  settings,
  onToggleSettings,
}: {
  settings: Settings;
  onToggleSettings: () => void;
}) {
  const accounts = [
    {
      platform: "chesscom",
      enabled: settings.chesscom_enabled,
      username: settings.chesscom_username,
      syncedAt: settings.chesscom_last_synced_at,
    },
    {
      platform: "lichess",
      enabled: settings.lichess_enabled,
      username: settings.lichess_username,
      syncedAt: settings.lichess_last_synced_at,
    },
  ].filter((account) => account.enabled);

  return (
    <p className="muted account">
      {accounts.map((account) => (
        <span key={account.platform}>
          {platformName(account.platform)} <strong>{account.username}</strong>{" "}
          synced {relativeTime(account.syncedAt)} ·{" "}
        </span>
      ))}
      <button className="link" onClick={onToggleSettings}>
        settings
      </button>
    </p>
  );
}

/** Server-side narrowing (see `GameFilters`), so the count beneath the table
 *  keeps meaning what it says. */
function Filters({
  filters,
  onChange,
}: {
  filters: GameFilters;
  onChange: (filters: GameFilters) => void;
}) {
  function update<K extends keyof GameFilters>(key: K, value: GameFilters[K]) {
    const next = { ...filters };
    if (value) next[key] = value;
    else delete next[key];
    onChange(next);
  }

  return (
    <div className="filters">
      <label>
        Colour
        <select
          value={filters.color ?? ""}
          onChange={(event) =>
            update("color", (event.target.value || undefined) as GameFilters["color"])
          }
        >
          <option value="">Any</option>
          <option value="white">White</option>
          <option value="black">Black</option>
        </select>
      </label>

      <label>
        Result
        <select
          value={filters.result ?? ""}
          onChange={(event) =>
            update("result", (event.target.value || undefined) as GameFilters["result"])
          }
        >
          <option value="">Any</option>
          <option value="win">Win</option>
          <option value="loss">Loss</option>
          <option value="draw">Draw</option>
        </select>
      </label>

      <label>
        Time
        <select
          value={filters.timeClass ?? ""}
          onChange={(event) =>
            update(
              "timeClass",
              (event.target.value || undefined) as GameFilters["timeClass"],
            )
          }
        >
          <option value="">Any</option>
          <option value="bullet">Bullet</option>
          <option value="blitz">Blitz</option>
          <option value="rapid">Rapid</option>
          <option value="daily">Daily</option>
        </select>
      </label>

      <label>
        <input
          type="checkbox"
          checked={filters.withErrors ?? false}
          onChange={(event) => update("withErrors", event.target.checked || undefined)}
        />
        Only games I erred in
      </label>
    </div>
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
  onClose,
}: {
  settings: Settings;
  onSaved: (settings: Settings) => void;
  onError: (message: string) => void;
  onClose: () => void;
}) {
  const [chesscom, setChesscom] = useState(settings.chesscom_enabled);
  const [chesscomUser, setChesscomUser] = useState(settings.chesscom_username ?? "");
  const [lichess, setLichess] = useState(settings.lichess_enabled);
  const [lichessUser, setLichessUser] = useState(settings.lichess_username ?? "");
  /** Always starts blank: a stored token is never sent back, so there is
   *  nothing to prefill it with. */
  const [token, setToken] = useState("");
  const [forgetToken, setForgetToken] = useState(false);
  const [depth, setDepth] = useState(settings.analysis_depth);
  const [background, setBackground] = useState(settings.background_analysis);
  const [saving, setSaving] = useState(false);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    try {
      const saved = await api.saveSettings({
        chesscom_enabled: chesscom,
        chesscom_username: chesscomUser.trim() || null,
        lichess_enabled: lichess,
        lichess_username: lichessUser.trim() || null,
        // Omitted keeps whatever is stored; "" is the explicit forget.
        ...(token.trim()
          ? { lichess_token: token.trim() }
          : forgetToken
            ? { lichess_token: "" }
            : {}),
        analysis_depth: depth,
        background_analysis: background,
      });
      onClose();
      onSaved(saved);
    } catch (err) {
      onError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="settings panel" onSubmit={save}>
      <h2>Settings</h2>
      {!isConnected(settings) && (
        <p>Add an account to get started — either platform, or both.</p>
      )}

      {/* Each platform is its own block. The two are independent, and in one
          flat list of fields a username read as belonging to whichever
          checkbox happened to sit above it. */}
      <div className="platform">
        <label>
          <input
            type="checkbox"
            checked={chesscom}
            onChange={(event) => setChesscom(event.target.checked)}
          />
          Chess.com
        </label>
        <label>
          Username
          <input
            value={chesscomUser}
            onChange={(event) => setChesscomUser(event.target.value)}
            placeholder="your Chess.com username"
            autoComplete="off"
          />
        </label>
      </div>

      <div className="platform">
        <label>
          <input
            type="checkbox"
            checked={lichess}
            onChange={(event) => setLichess(event.target.checked)}
          />
          Lichess
        </label>
        <label>
          Username
          <input
            value={lichessUser}
            onChange={(event) => setLichessUser(event.target.value)}
            placeholder="your Lichess username"
            autoComplete="off"
          />
        </label>
        <label>
          API token
          <input
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            disabled={forgetToken}
            placeholder={
              settings.lichess_token_set
                ? "stored — leave blank to keep it"
                : "optional"
            }
            autoComplete="off"
          />
        </label>
        <p className="muted">
          Optional, and only yours: generate one under Preferences → API access
          tokens on lichess.org. It raises the rate limits, is stored locally
          and is never shown again.
        </p>
        {settings.lichess_token_set && (
          <label>
            <input
              type="checkbox"
              checked={forgetToken}
              onChange={(event) => setForgetToken(event.target.checked)}
            />
            Forget the stored token
          </label>
        )}
      </div>

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
      <div className="settings-actions">
        <button type="submit" disabled={saving}>
          {saving ? "Checking…" : "Save"}
        </button>
        {isConnected(settings) && (
          <button type="button" className="link" onClick={onClose}>
            Cancel
          </button>
        )}
      </div>
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

  const running = status.queued + (status.current_game_id === null ? 0 : 1);
  if (running === 0) return null;

  return (
    <div className="progress">
      {/* Only the game count is announced. It changes once per game, where the
          position counter underneath moves every second or two — a live region
          around that would talk over everything else on the page. */}
      <span className="muted" role="status">
        Analysing {running} game{running === 1 ? "" : "s"}…
      </span>
      {status.current_total > 0 && (
        <progress
          value={status.current_ply}
          max={status.current_total}
          aria-label="Positions analysed in the current game"
        />
      )}
    </div>
  );
}

function GameTable({
  games,
  filtered,
  onOpen,
  onRetry,
}: {
  games: GameList | null;
  filtered: boolean;
  onOpen: (id: number) => void;
  onRetry: (id: number) => void;
}) {
  if (!games) return <p className="muted">Loading games…</p>;
  if (games.total === 0) {
    return (
      <p className="muted">
        {filtered
          ? "No games match these filters."
          : "No games yet. Hit “Sync now”."}
      </p>
    );
  }

  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Date</th>
            {/* Two platforms land in one list, so the row has to say which. */}
            <th>Site</th>
            <th>Colour</th>
            <th>Opponent</th>
            <th>Result</th>
            <th>Time</th>
            <th>ECO</th>
            <th>Errors</th>
            <th>Accuracy</th>
          </tr>
        </thead>
        <tbody>
          {games.games.map((game) => (
            <tr
              key={game.id}
              className="clickable"
              // The row is the mouse target; the link in it is the real control,
              // so a click that landed on the link or the retry button is already
              // handled and must not fire twice.
              onClick={(event) => {
                if ((event.target as HTMLElement).closest("a, button")) return;
                onOpen(game.id);
              }}
            >
              <td>
                <a className="game-link" href={`#/game/${game.id}`}>
                  <span className="wide-only">{shortDate(game.played_at)}</span>
                  <span className="narrow-only">{compactDate(game.played_at)}</span>
                </a>
              </td>
              <td>{platformName(game.platform)}</td>
              <td>{game.player_color ?? "—"}</td>
              <td>{game.opponent ?? "—"}</td>
              <td className={game.result ?? undefined}>{game.result ?? "—"}</td>
              <td>{timeControl(game.time_control)}</td>
              <td>{game.eco_code ?? "—"}</td>
              <td>{game.analysis ? <ErrorCounts summary={game.analysis} /> : null}</td>
              {/* Accuracy once there is one; until then the reason there is not,
                  which is the same column's worth of information. */}
              <td className="numeric">
                {game.analysis ? (
                  `${accuracyPercent(game.analysis)}%`
                ) : (
                  <AnalysisBadge
                    status={game.analysis_status}
                    onRetry={() => onRetry(game.id)}
                  />
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
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
