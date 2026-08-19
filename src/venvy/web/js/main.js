/**
 * Application entry point.
 *
 * Imports every feature module for its side effects (event listeners, timers)
 * and republishes the handler functions that index.html references from inline
 * `onclick` attributes.
 *
 * That window bridge is deliberate and is the only thing here that touches the
 * global scope. Inline handlers resolve against `window`, and module scope is
 * not global, so a module-only build would silently break every button. The
 * list below is generated from the attributes actually present in index.html —
 * if you add an inline handler, add its export here too.
 */

import "./core/api.js";
import "./core/state.js";
import "./core/dom.js";
import { closeSidebar, toggleSidebar } from "./ui/sidebar.js";
import "./ui/swipe.js";
import { fileOp, switchTab } from "./ui/tabs.js";
import { closeModal, openModal } from "./ui/modals.js";
import { closeCtxMenu } from "./ui/contextmenu.js";
import "./features/editor.js";
import "./features/polling.js";
import { cancelTotpStep, changePassword, confirmTotpSetup, copyTotpSecret, disableTotp, logout, openAccountMenu, openChangePwdModal, openTotpSetup } from "./features/auth.js";
import { confirmDeleteUser, createUser, openUsersModal } from "./features/users.js";
import { controlApp, openExternal, setOpenUrlPref } from "./features/apps.js";
import { _termClear, _termReconnect, termMobKey, termMobToggleMore, termModToggle, termSend } from "./features/terminal.js";
import { aceSearch, closeEditorMobile, confirmFileNameOp, downloadCurrentFile, loadFiles, promptFileOp, saveCurrentFile, uploadFile } from "./features/files.js";
import { confirmRemovePackage, copyLogs, deleteLogs, installPackage } from "./features/logs.js";
import { installAllMissing, scanImports } from "./features/packages.js";
import { closeSystemView, loadSystemView, openSystemView } from "./features/system.js";
import { confirmPublishOverwrite, copyCfRouteUrl, publishCfRoute, saveCfCreds, testCfCreds, toggleCfGuide, toggleCfTokenVis, unpublishCfRoute, updateCfHostnamePreview } from "./features/cloudflare.js";
import { confirmDeleteApp, deleteApp, downloadAppZip, onRuntimeChange, saveSettings } from "./features/settings.js";
import "./features/createapp.js";
import { dismissPwaBanner, triggerPwaInstall } from "./core/pwa.js";

// ── window bridge for index.html's inline event attributes ──────────────────
Object.assign(window, {
    // ui/sidebar
    closeSidebar, toggleSidebar,
    // ui/tabs
    fileOp, switchTab,
    // ui/modals
    closeModal, openModal,
    // ui/contextmenu
    closeCtxMenu,
    // features/auth
    cancelTotpStep, changePassword, confirmTotpSetup, copyTotpSecret, disableTotp, logout, openAccountMenu, openChangePwdModal, openTotpSetup,
    // features/users
    confirmDeleteUser, createUser, openUsersModal,
    // features/apps
    controlApp, openExternal, setOpenUrlPref,
    // features/terminal
    _termClear, _termReconnect, termMobKey, termMobToggleMore, termModToggle, termSend,
    // features/files
    aceSearch, closeEditorMobile, confirmFileNameOp, downloadCurrentFile, loadFiles, promptFileOp, saveCurrentFile, uploadFile,
    // features/logs
    confirmRemovePackage, copyLogs, deleteLogs, installPackage,
    // features/packages
    installAllMissing, scanImports,
    // features/system
    closeSystemView, loadSystemView, openSystemView,
    // features/cloudflare
    confirmPublishOverwrite, copyCfRouteUrl, publishCfRoute, saveCfCreds, testCfCreds, toggleCfGuide, toggleCfTokenVis, unpublishCfRoute, updateCfHostnamePreview,
    // features/settings
    confirmDeleteApp, deleteApp, downloadAppZip, onRuntimeChange, saveSettings,
    // core/pwa
    dismissPwaBanner, triggerPwaInstall,
});
