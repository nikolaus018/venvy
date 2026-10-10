// _featherReplace(): feather.replace() that tolerates the icon library failing to load.
        // Safe wrapper for _featherReplace() — gracefully handles offline / CDN failure
        window._featherReplace = function () {
            try { if (window.feather) feather.replace(); } catch (e) { }
        };
