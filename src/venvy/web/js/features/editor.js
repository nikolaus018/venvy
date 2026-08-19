/**
 * The Ace code editor instance used by the file browser.
 */

import { state } from "../core/state.js";
import { _setEditorDirty } from "./files.js";

export function initAceEditor() {
    if (state.aceEditor) return;
    state.aceEditor = ace.edit("file-editor");
    state.aceEditor.setTheme("ace/theme/one_dark");
    state.aceEditor.session.setMode("ace/mode/python");
    state.aceEditor.setFontSize(window.innerWidth < 768 ? 12 : 13);
    state.aceEditor.setOption("showPrintMargin", false);
    state.aceEditor.setOption("wrap", false);
    state.aceEditor.renderer.setScrollMargin(6, 6);
    // Custom theme overrides for dark look
    state.aceEditor.container.style.background = 'var(--surface)';
    state.aceEditor.on('change', () => { if (state._openFilePath) _setEditorDirty(true); });
    // Note: ext-searchbox registers its own Ctrl-F handler — no override needed.
}
