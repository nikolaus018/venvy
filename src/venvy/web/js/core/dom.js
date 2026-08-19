/**
 * Small DOM helpers shared by every view: escaping and toast notifications.
 */

export function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function showToast(msg, type = 'info', duration = 3000) {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = msg;
    container.appendChild(toast);
    requestAnimationFrame(() => requestAnimationFrame(() => toast.classList.add('show')));
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 250);
    }, duration);
}

/**
 * Re-render Feather icons after DOM changes.
 *
 * Wrapped so a blocked or failed CDN load degrades to "no icons" rather than
 * throwing out of whatever view just rendered.
 */
export function _featherReplace() {
    try {
        if (window.feather) window.feather.replace();
    } catch {
        /* icons are decorative — never let this break a render */
    }
}
