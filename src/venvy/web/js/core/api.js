/**
 * API base URL and the CSRF-aware fetch wrapper.
 *
 * Every state-changing request must echo the `csrf_token` cookie back in the
 * `x-csrf-token` header. Rather than making each call site remember, this
 * module patches `window.fetch` once so the whole app gets it for free.
 */

export const API = window.location.origin + "/api";

export const originalFetch = window.fetch;

window.fetch = async function (resource, init) {
    init = init || {};
    const method = (init.method || 'GET').toUpperCase();
    if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) {
        init.headers = init.headers || {};
        const tokenMatch = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/);
        if (tokenMatch) {
            init.headers['x-csrf-token'] = decodeURIComponent(tokenMatch[1]);
        }
    }
    return originalFetch.call(this, resource, init);
};
