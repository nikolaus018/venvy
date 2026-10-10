// Drops Tailwind's 'should not be used in production' console warning. Must load before Tailwind.
        const _warn = console.warn;
        console.warn = function (...args) {
            if (args[0] && typeof args[0] === 'string' && args[0].includes('cdn.tailwindcss.com should not be used in production')) return;
            _warn.apply(console, args);
        };
