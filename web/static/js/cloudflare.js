// Cloudflare: credentials card and per-app tunnel publishing.
// A classic script: it shares one global scope with the other files in this
// directory, in the order web/index.html loads them.
        // ── Cloudflare ────────────────────────────────────────────────────────────
        function _cfStrip(s) {
            if (!s) return '';
            return s.replace(/[\u200B-\u200D\uFEFF\u00A0\u2028\u2029]/g, '').replace(/[\r\n\t]/g, '').trim();
        }
        function toggleCfTokenVis(inputId, iconId) {
            const inp = document.getElementById(inputId), icon = document.getElementById(iconId);
            const show = inp.type === 'password'; inp.type = show ? 'text' : 'password';
            icon.innerHTML = show
                ? '<path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'
                : '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
        }
        function toggleCfGuide() {
            const body = document.getElementById('cf-guide-body'), chevron = document.getElementById('cf-guide-chevron');
            const open = body.classList.toggle('hidden');
            chevron.style.transform = open ? '' : 'rotate(180deg)';
        }
        async function loadCfCredsStatus() {
            try {
                const res = await fetch(`${API}/system/cloudflare/status`);
                if (!res.ok) return;
                const st = await res.json();
                const allSet = !!st.configured;
                // Warm the shared cache with what we just fetched
                _cfCredsCache = { hasCreds: allSet, ts: Date.now() };
                const badge = document.getElementById('cf-creds-badge');
                if (allSet) {
                    badge.classList.remove('hidden'); badge.classList.add('flex');
                    document.getElementById('cf-cred-token').placeholder = '••••••••  (saved — paste to replace)';
                    document.getElementById('cf-cred-account').placeholder = st.account_id_hint + '…  (saved)';
                    document.getElementById('cf-cred-tunnel').placeholder = st.tunnel_id_hint + '…  (saved)';
                } else {
                    badge.classList.add('hidden'); badge.classList.remove('flex');
                }
            } catch (e) { }
        }
        async function saveCfCreds() {
            _cfZonesCache = null; // Invalidate zone cache
            _cfCredsCache = null; // Invalidate creds cache
            const tokenInput = document.getElementById('cf-cred-token');
            const accountInput = document.getElementById('cf-cred-account');
            const tunnelInput = document.getElementById('cf-cred-tunnel');

            const token = _cfStrip(tokenInput.value);
            const account = _cfStrip(accountInput.value);
            const tunnel = _cfStrip(tunnelInput.value);

            // Require all three fields — partial saves make an unusable config
            const missing = [];
            if (!token) missing.push('API Token');
            if (!account) missing.push('Account ID');
            if (!tunnel) missing.push('Tunnel ID');
            if (missing.length) {
                showToast(`Fill in: ${missing.join(', ')}`, 'warn', 4000);
                (!token ? tokenInput : !account ? accountInput : tunnelInput).focus();
                return;
            }

            const btn = document.getElementById('cf-creds-save-btn');
            btn.disabled = true; btn.textContent = 'Saving…';

            try {
                // Single batch request — one DB transaction, no sequential round trips
                const res = await fetch(`${API}/system/settings/batch`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify([
                        { key: 'cf_api_token', value: token },
                        { key: 'cf_account_id', value: account },
                        { key: 'cf_tunnel_id', value: tunnel },
                    ])
                });
                if (!res.ok) throw new Error(`Save failed (${res.status})`);

                tokenInput.value = '';
                accountInput.value = '';
                tunnelInput.value = '';

                showToast('Cloudflare credentials saved.', 'success');
                await loadCfCredsStatus();
            } catch (e) {
                showToast('Save failed: ' + e.message, 'error', 5000);
            } finally {
                btn.disabled = false;
                btn.textContent = 'Save';
            }
        }
        async function testCfCreds() {
            const btn = document.getElementById('cf-creds-test-btn');
            btn.disabled = true;
            btn.innerHTML = '<svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> Testing…';
            document.getElementById('cf-test-results').classList.add('hidden');
            document.getElementById('cf-test-permission-hint').classList.add('hidden');
            document.getElementById('cf-test-connector-warn')?.classList.add('hidden');
            try {
                const res = await fetch(`${API}/system/cloudflare/test`, { method: 'POST' });
                const data = await res.json();
                document.getElementById('cf-test-results').classList.remove('hidden');

                /**
                 * Render a single test result row.
                 * @param {string} id - Element ID prefix (e.g. 'cf-test-account')
                 * @param {boolean} ok - Whether the check passed
                 * @param {string} text - Display text
                 */
                const _set = (id, ok, text) => {
                    const el = document.getElementById(id);
                    const iconEl = document.getElementById(id + '-icon');
                    const textEl = document.getElementById(id + '-text');
                    if (!el) return;
                    el.style.cssText = ok
                        ? 'background:rgba(16,185,129,0.08);border:1px solid rgba(16,185,129,0.15);color:var(--green);padding:0.625rem 0.75rem;border-radius:12px;display:flex;align-items:flex-start;gap:0.625rem'
                        : 'background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.15);color:var(--red);padding:0.625rem 0.75rem;border-radius:12px;display:flex;align-items:flex-start;gap:0.625rem';
                    iconEl.innerHTML = ok
                        ? '<svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>'
                        : '<svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
                    textEl.textContent = text;
                };

                const checks = data.checks || {};
                const acct = checks.account || {};
                const tun = checks.tunnel || {};
                const dns = checks.dns_zones || {};

                _set('cf-test-account',
                    acct.ok,
                    acct.ok ? `Account OK — ${acct.name || 'verified'}` : `Account: ${acct.error || 'failed'}`
                );

                // Show tunnel name + active connections; warn if connector not running
                let tunText;
                if (tun.ok) {
                    tunText = `Tunnel OK — ${tun.name || tun.id || 'verified'}`;
                    if (typeof tun.active_connections === 'number') {
                        tunText += ` · ${tun.active_connections} active connection${tun.active_connections === 1 ? '' : 's'}`;
                    }
                    if (tun.active_connections === 0) {
                        tunText += ' ⚠ cloudflared not running';
                    }
                } else {
                    tunText = `Tunnel: ${tun.error || 'failed'}`;
                }
                _set('cf-test-tunnel', tun.ok && (tun.active_connections ?? 1) > 0, tunText);

                // Show connector warning inline if 0 active connections
                if (tun.ok && tun.active_connections === 0) {
                    document.getElementById('cf-test-permission-hint').classList.add('hidden');
                    // connector-warn removed — tunnel 0-connections shown inline in tunnel row
                }

                _set('cf-test-dns-zones',
                    dns.ok,
                    dns.ok ? (dns.message || 'DNS zones accessible') : `DNS zones: ${dns.error || 'failed — add Zone:Read + DNS:Edit to your token'}`
                );

                const anyFailed = !acct.ok || !tun.ok || !dns.ok;
                if (anyFailed) {
                    const allText = JSON.stringify(checks).toLowerCase();
                    if (allText.includes('403') || allText.includes('unauthorized') || allText.includes('permission') || !dns.ok) {
                        document.getElementById('cf-test-permission-hint').classList.remove('hidden');
                        document.getElementById('cf-test-connector-warn')?.classList.add('hidden');
                    }
                }
            } catch (e) { showToast('Test failed: ' + e.message, 'error'); }
            finally {
                btn.disabled = false;
                btn.innerHTML = '<svg class="w-4 h-4" style="color:var(--text)" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Test';
            }
        }

        /**
         * Render the Cloudflare tunnel card into one of three states:
         *   • no-creds  — warn, hide everything else
         *   • unpublished — show hostname input + Publish button
         *   • published   — show live URL banner + hostname input (editable) + Republish + Remove buttons
         *
         * Always resets ALL button states so switching apps never leaves a stale spinner.
         */
        // Cache to avoid re-fetching CF creds on every tab switch / card render.
        // Invalidated on save and on explicit test.
        let _cfCredsCache = null;  // { hasCreds, tokenValue, accountValue, tunnelValue, ts }
        const _CF_CREDS_TTL_MS = 30_000;  // 30 seconds

        async function _loadCfCreds() {
            const now = Date.now();
            if (_cfCredsCache && (now - _cfCredsCache.ts) < _CF_CREDS_TTL_MS) {
                return _cfCredsCache;
            }
            // The status endpoint is open to operators (who may publish routes)
            // and never returns the API token itself.
            const st = await fetch(`${API}/system/cloudflare/status`)
                .then(r => r.ok ? r.json() : {}).catch(() => ({}));
            _cfCredsCache = {
                hasCreds: !!st.configured,
                ts: Date.now(),
            };
            return _cfCredsCache;
        }

        // Per-app tunnel route cache — keyed by app_id, 30s TTL.
        // Invalidated on publish/unpublish so it always reflects real state.
        const _cfRouteCache = {};  // { [appId]: { data, ts } }
        const _CF_ROUTE_TTL_MS = 30_000;

        async function _loadCfRoute(appId) {
            const now = Date.now();
            const cached = _cfRouteCache[appId];
            if (cached && (now - cached.ts) < _CF_ROUTE_TTL_MS) return cached.data;
            const data = await fetch(`${API}/apps/${appId}/tunnel`)
                .then(r => r.ok ? r.json() : null).catch(() => null);
            _cfRouteCache[appId] = { data, ts: Date.now() };
            return data;
        }

        function _cfRouteInvalidate(appId) {
            delete _cfRouteCache[appId];
        }

        async function loadCfRouteStatus() {
            if (!activeAppId) return;

            // Grab all elements up front
            const noCredsWarn = document.getElementById('cf-no-creds-warn');
            const noPortWarn = document.getElementById('cf-no-port-warn');
            const urlBanner = document.getElementById('cf-route-url-banner');
            const badge = document.getElementById('cf-route-badge');
            const hostnameRow = document.getElementById('cf-hostname-row');
            const subdomainInput = document.getElementById('cf-subdomain-input');
            const domainSelect = document.getElementById('cf-domain-select');
            const publishBtn = document.getElementById('cf-publish-btn');
            const republishBtn = document.getElementById('cf-republish-btn');
            const unpublishBtn = document.getElementById('cf-unpublish-btn');

            const inputsSection = document.getElementById('cf-inputs-section');

            // ── Reset everything to a clean baseline ─────────────────────────────
            [noCredsWarn, noPortWarn, urlBanner].forEach(el => {
                el.classList.add('hidden'); el.classList.remove('flex');
            });
            badge.classList.add('hidden'); badge.classList.remove('flex');
            hostnameRow.classList.remove('hidden');
            inputsSection.classList.remove('hidden');

            // Always reset all three buttons — no stale disabled/spinner state
            publishBtn.classList.remove('hidden');
            publishBtn.disabled = false;
            publishBtn.style.display = 'flex';
            publishBtn.innerHTML = '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><path d="M16.5 7.5C16.5 7.5 15.75 4.5 12 4.5C8.68 4.5 6 7.18 6 10.5C6 10.5 3 11.25 3 14.25C3 16.93 5.07 19.5 8.25 19.5H17.25C19.73 19.5 21.75 17.48 21.75 15C21.75 12.8 20.12 10.98 18 10.58"/></svg> Publish';

            republishBtn.classList.remove('hidden');
            republishBtn.disabled = false;
            republishBtn.style.display = 'none';
            republishBtn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.85"/></svg> Republish';

            unpublishBtn.classList.remove('hidden');
            unpublishBtn.disabled = false;
            unpublishBtn.style.display = 'none';
            unpublishBtn.innerHTML = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg> Remove';

            // Show loading skeleton immediately so card isn't blank
            noCredsWarn.classList.add('hidden');
            noPortWarn.classList.add('hidden');
            urlBanner.classList.add('hidden');
            badge.classList.add('hidden');
            inputsSection.classList.add('hidden');
            noCredsWarn.classList.remove('flex');

            // Show a subtle spinner in the card while both fetches complete
            const cfCard = document.getElementById('cf-no-creds-warn')?.closest('.card, [class*="rounded"]') ||
                noCredsWarn?.parentElement;
            let _skeleton = document.getElementById('_cf-loading-skeleton');
            if (!_skeleton) {
                _skeleton = document.createElement('div');
                _skeleton.id = '_cf-loading-skeleton';
                _skeleton.className = 'flex items-center gap-2 py-2';
                _skeleton.style.color = 'var(--text3)';
                _skeleton.innerHTML = '<svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg><span class="text-xs">Checking tunnel status…</span>';
                noCredsWarn?.parentElement?.insertBefore(_skeleton, noCredsWarn);
            }
            _skeleton.classList.remove('hidden');

            try {
                // Both fetches run in parallel; creds uses 30s cache, route uses 30s cache.
                const [creds, tunnelData] = await Promise.all([
                    _loadCfCreds(),
                    _loadCfRoute(activeAppId),
                ]);
                const routeRes = tunnelData?.routes?.find(r => r.local_port === activeApp.port);

                const hasCreds = creds.hasCreds;
                if (!hasCreds) {
                    // State: no credentials configured — show warning, hide ALL inputs
                    noCredsWarn.classList.remove('hidden'); noCredsWarn.classList.add('flex');
                    inputsSection.classList.add('hidden');
                    return;
                }

                if (!activeApp?.port) {
                    noPortWarn.classList.remove('hidden'); noPortWarn.classList.add('flex');
                }

                // ── Load domain zones into the dropdown ────────────────────────────
                await _loadCfZonesDropdown(domainSelect);

                const isPublished = !!(routeRes?.published && routeRes?.hostname);

                inputsSection.classList.remove('hidden');

                if (isPublished) {
                    // State: published — show live URL, Republish + Remove buttons
                    _setCfHostnameFields(routeRes.hostname, subdomainInput, domainSelect);

                    const url = `https://${routeRes.hostname}`;
                    document.getElementById('cf-route-url-link').textContent = url;
                    document.getElementById('cf-route-url-link').href = url;
                    document.getElementById('cf-route-url-open').href = url;
                    urlBanner.classList.remove('hidden');

                    badge.classList.remove('hidden'); badge.classList.add('flex');

                    publishBtn.classList.add('hidden');
                    republishBtn.classList.remove('hidden');
                    republishBtn.style.display = 'flex';
                    unpublishBtn.classList.remove('hidden');
                    unpublishBtn.style.display = 'flex';
                } else {
                    // State: unpublished — show Publish button only
                    if (routeRes?.hostname) {
                        _setCfHostnameFields(routeRes.hostname, subdomainInput, domainSelect);
                    }
                    publishBtn.classList.remove('hidden');
                    publishBtn.style.display = 'flex';
                    republishBtn.classList.add('hidden');
                    republishBtn.style.display = 'none';
                    unpublishBtn.classList.add('hidden');
                    unpublishBtn.style.display = 'none';
                }
                updateCfHostnamePreview();
            } catch (e) {
                // On network failure just leave the reset baseline (Publish visible)
                /* cf route status error */
            } finally {
                // Always hide the loading skeleton regardless of outcome
                const sk = document.getElementById('_cf-loading-skeleton');
                if (sk) sk.classList.add('hidden');
            }
        }

        /**
         * Load available Cloudflare zones from the API and populate the domain <select>.
         * Reuses cached list within the same session to avoid redundant API calls.
         */
        let _cfZonesCache = null;
        async function _loadCfZonesDropdown(selectEl) {
            if (!selectEl) return;
            const currentVal = selectEl.value;

            // Use cache if available
            if (_cfZonesCache) {
                _populateZoneSelect(selectEl, _cfZonesCache, currentVal);
                return;
            }

            selectEl.innerHTML = '<option value="" disabled selected>loading domains…</option>';
            try {
                const res = await fetch(`${API}/system/cloudflare/zones`);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                if (!data.ok || !data.zones?.length) {
                    selectEl.innerHTML = '<option value="" disabled selected>no domains found</option>';
                    return;
                }
                _cfZonesCache = data.zones;
                _populateZoneSelect(selectEl, data.zones, currentVal);
            } catch (e) {
                selectEl.innerHTML = '<option value="" disabled selected>failed to load domains</option>';
                /* cf zones error */
            }
        }

        function _populateZoneSelect(selectEl, zones, preferredValue) {
            selectEl.innerHTML = '';
            const placeholder = document.createElement('option');
            placeholder.value = '';
            placeholder.disabled = true;
            placeholder.textContent = 'Select domain…';
            placeholder.style.color = 'var(--text3)';
            selectEl.appendChild(placeholder);

            for (const z of zones) {
                const opt = document.createElement('option');
                opt.value = z.name;
                opt.textContent = z.name;
                selectEl.appendChild(opt);
            }

            // Restore previous selection if it still exists, else select first real option
            if (preferredValue && zones.some(z => z.name === preferredValue)) {
                selectEl.value = preferredValue;
            } else if (zones.length) {
                selectEl.value = zones[0].name;
            }
        }

        /**
         * Split a full hostname like "app.example.com" into subdomain="app", domain="example.com"
         * and set the two input fields accordingly.  For an apex domain "example.com", subdomain="".
         */
        function _setCfHostnameFields(hostname, subdomainInput, domainSelect) {
            if (!hostname) return;
            // Find the longest matching zone name in the dropdown options
            const options = Array.from(domainSelect.options).map(o => o.value).filter(Boolean);
            let matchedDomain = '';
            for (const opt of options) {
                if (hostname === opt || hostname.endsWith('.' + opt)) {
                    if (opt.length > matchedDomain.length) matchedDomain = opt;
                }
            }
            if (matchedDomain) {
                domainSelect.value = matchedDomain;
                subdomainInput.value = hostname === matchedDomain ? '' : hostname.slice(0, hostname.length - matchedDomain.length - 1);
            } else {
                // Zone not in dropdown — put full hostname in subdomain field as fallback
                subdomainInput.value = hostname;
            }
        }

        /** Build the full hostname from subdomain input + domain select and show the preview. */
        function updateCfHostnamePreview() {
            const subdomain = (document.getElementById('cf-subdomain-input')?.value || '').trim().toLowerCase();
            const domain = (document.getElementById('cf-domain-select')?.value || '').trim().toLowerCase();
            const preview = document.getElementById('cf-hostname-preview');
            if (!preview) return;
            if (!domain) { preview.textContent = ''; return; }
            const full = subdomain ? `${subdomain}.${domain}` : domain;
            preview.textContent = `https://${full}`;
        }

        /** Read the composed hostname from the two fields. */
        function _getCfHostname() {
            const subdomain = (document.getElementById('cf-subdomain-input')?.value || '').trim().toLowerCase();
            const domain = (document.getElementById('cf-domain-select')?.value || '').trim().toLowerCase();
            if (!domain) return '';
            return subdomain ? `${subdomain}.${domain}` : domain;
        }

        /** Publish (first time) or Republish (update hostname) — with conflict pre-check */
        async function publishCfRoute() {
            if (!activeAppId) return;
            const hostname = _getCfHostname();
            if (!hostname) { showToast('Select a domain first.', 'warn'); return; }

            // Guard: app must have a port set for the tunnel to route traffic anywhere
            if (!activeApp?.port) {
                showToast('Set a port for this app in Settings → Networking first.', 'warn', 5000);
                switchTab('settings');
                return;
            }

            const isRepublish = !document.getElementById('cf-republish-btn').classList.contains('hidden');
            const activeBtn = isRepublish
                ? document.getElementById('cf-republish-btn')
                : document.getElementById('cf-publish-btn');

            // If republishing to the exact same hostname we already own, skip conflict check
            const currentHostname = document.getElementById('cf-route-url-link')?.textContent?.replace('https://', '').trim();
            const isSameHostname = isRepublish && currentHostname === hostname;

            if (!isSameHostname) {
                // Pre-flight: check for hostname conflicts before doing anything
                const originalHTML = activeBtn.innerHTML;
                activeBtn.disabled = true;
                activeBtn.innerHTML = '<svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> Checking…';
                try {
                    const chk = await fetch(`${API}/apps/${activeAppId}/tunnel/check?hostname=${encodeURIComponent(hostname)}&local_port=${activeApp.port}`);
                    const chkData = await chk.json().catch(() => ({}));
                    if (chk.ok && chkData.conflict) {
                        // Restore button, show conflict modal
                        activeBtn.disabled = false;
                        activeBtn.innerHTML = originalHTML;
                        document.getElementById('tunnel-conflict-msg').textContent = chkData.reason || 'This hostname is already in use.';
                        document.getElementById('tunnel-conflict-modal').dataset.hostname = hostname;
                        openModal('tunnel-conflict-modal');
                        return;
                    }
                } catch (e) {
                    // Network error on check — restore button and proceed anyway
                    activeBtn.disabled = false;
                    activeBtn.innerHTML = originalHTML;
                }
                activeBtn.disabled = false;
                activeBtn.innerHTML = originalHTML;
            }

            await _doPublish(hostname, isRepublish);
        }

        /** Called when user clicks Overwrite in the conflict modal */
        async function confirmPublishOverwrite() {
            const hostname = document.getElementById('tunnel-conflict-modal').dataset.hostname;
            closeModal('tunnel-conflict-modal');
            if (!hostname) return;
            const isRepublish = !document.getElementById('cf-republish-btn').classList.contains('hidden');
            await _doPublish(hostname, isRepublish);
        }

        /** Core publish logic — shared by publishCfRoute and confirmPublishOverwrite */
        async function _doPublish(hostname, isRepublish) {
            const activeBtn = isRepublish
                ? document.getElementById('cf-republish-btn')
                : document.getElementById('cf-publish-btn');
            const removeBtn = document.getElementById('cf-unpublish-btn');

            const originalHTML = activeBtn.innerHTML;
            activeBtn.disabled = true;
            activeBtn.innerHTML = '<svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> ' + (isRepublish ? 'Republishing…' : 'Publishing…');
            if (removeBtn) removeBtn.disabled = true;

            let publishOk = false;
            try {
                // 30 s hard timeout — Cloudflare API can be slow
                const controller = new AbortController();
                const tId = setTimeout(() => controller.abort(), 30000);
                let res;
                try {
                    res = await fetch(`${API}/apps/${activeAppId}/tunnel/publish`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ hostname, local_port: activeApp.port }),
                        signal: controller.signal,
                    });
                } finally {
                    clearTimeout(tId);
                }
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                publishOk = true;

                showToast(
                    isRepublish
                        ? `Republished → https://${hostname}. DNS may take a moment.`
                        : `Published → https://${hostname}. DNS may take a moment.`,
                    'success', 6000
                );

                const appsRes = await fetch(`${API}/apps`).catch(() => null);
                if (appsRes?.ok) {
                    const apps = await appsRes.json().catch(() => []);
                    const updated = apps.find(a => a.id === activeAppId);
                    if (updated) { activeApp = updated; _updateNetworkCard(updated); }
                }
            } catch (e) {
                const msg = e.name === 'AbortError' ? 'Publish timed out — Cloudflare may be slow.' : 'Publish failed: ' + e.message;
                showToast(msg, 'error', 7000);
            } finally {
                // Always restore buttons — whether success, error, or timeout
                activeBtn.disabled = false;
                activeBtn.innerHTML = originalHTML;
                if (removeBtn) removeBtn.disabled = false;
            }

            if (publishOk) { _cfRouteInvalidate(activeAppId); await loadCfRouteStatus(); }
        }

        /** Remove the published route + DNS record */
        async function unpublishCfRoute() {
            if (!activeAppId) return;
            const btn = document.getElementById('cf-unpublish-btn');
            const republishBtn = document.getElementById('cf-republish-btn');
            const originalHTML = btn.innerHTML;

            btn.disabled = true;
            btn.innerHTML = '<svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> Removing…';
            if (republishBtn) republishBtn.disabled = true;

            let removeOk = false;
            try {
                const controller = new AbortController();
                const tId = setTimeout(() => controller.abort(), 30000);
                let res;
                try {
                    res = await fetch(`${API}/apps/${activeAppId}/tunnel/publish?local_port=${activeApp.port}`, {
                        method: 'DELETE',
                        signal: controller.signal,
                    });
                } finally {
                    clearTimeout(tId);
                }
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
                removeOk = true;
                showToast('Route removed. DNS record deleted.', 'info');

                const appsRes = await fetch(`${API}/apps`).catch(() => null);
                if (appsRes?.ok) {
                    const apps = await appsRes.json().catch(() => []);
                    const updated = apps.find(a => a.id === activeAppId);
                    if (updated) { activeApp = updated; _updateNetworkCard(updated); }
                }
            } catch (e) {
                const msg = e.name === 'AbortError' ? 'Remove timed out.' : 'Remove failed: ' + e.message;
                showToast(msg, 'error', 5000);
            } finally {
                btn.disabled = false;
                btn.innerHTML = originalHTML;
                if (republishBtn) republishBtn.disabled = false;
            }

            if (removeOk) { _cfRouteInvalidate(activeAppId); await loadCfRouteStatus(); }
        }

        function copyCfRouteUrl() {
            const url = document.getElementById('cf-route-url-link')?.textContent?.trim();
            if (!url || url === '#') return;
            navigator.clipboard.writeText(url).catch(() => { const ta = document.createElement('textarea'); ta.value = url; ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); });
            showToast('URL copied!', 'success', 1500);
        }

        async function copyPortToClipboard(port) {
            try { await navigator.clipboard.writeText(String(port)); }
            catch (e) { const ta = document.createElement('textarea'); ta.value = String(port); document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta); }
            showToast(`Port ${port} copied!`, 'success', 1500);
        }
