/**
 * Application state shared between feature modules.
 *
 * A single mutable object rather than individual exports: ES module bindings
 * are read-only for importers, so `export let activeAppId` could be observed
 * from another module but never assigned. Everything here is genuinely
 * cross-cutting — state used by only one feature stays in that feature's
 * module, where it belongs.
 */

export const state = {
    // ── Current selection ────────────────────────────────────────────────────
    /** id of the app shown in the detail pane, or null on the dashboard. */
    activeAppId: null,
    /** The full app object for `activeAppId`, refreshed by polling. */
    activeApp: null,
    /** The signed-in user: { username, role, totp_enabled }. */
    currentUser: null,
    /** Most recent app list, so views can render without refetching. */
    _cachedApps: [],

    // ── Timers and sockets ───────────────────────────────────────────────────
    /** WebSocket carrying the terminal session, or null when closed. */
    termSocket: null,
    /** Interval polling the app list. */
    pollInterval: null,
    /** Interval tailing the active app's log. */
    logInterval: null,
    /** Interval refreshing the port map. */
    _portPollTimer: null,

    // ── File browser ─────────────────────────────────────────────────────────
    /** Directory currently shown, relative to the app root. */
    currentBrowserPath: "",
    /** Paths selected in the file tree. */
    selectedFiles: new Set(),
    /** Anchor for shift-click range selection. */
    lastSelectedPath: null,
    /** Pending cut/copy: { op, paths, filenames } or null. */
    fileClipboard: null,
    /** What the context menu was opened on. */
    ctxTarget: null,

    // ── Editor ───────────────────────────────────────────────────────────────
    /** The Ace editor instance, created lazily on first use. */
    aceEditor: null,
    /** Path of the file open in the editor. */
    _openFilePath: null,
    /** Whether the editor holds unsaved changes. */
    _editorDirty: false,
    /** Whether the mobile full-screen editor is showing. */
    isMobileFileEditorOpen: false,

    // ── Transient UI ─────────────────────────────────────────────────────────
    /** Unsaved "open URL" preference, applied when settings are saved. */
    _tempOpenUrlPref: null,
};
