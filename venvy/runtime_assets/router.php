<?php
// Router behind Venvy's "PHP" runtime, used with `php -S` when the app names
// no router script of its own. Files are served by PHP itself (return false:
// .php files run, everything else is sent as it is). What this adds:
//   * dotfiles, dot-folders (except .well-known), Venvy's logs/ and vendor/
//     are never served (including via // double-slash URIs or internal symlinks)
//   * composer.json / composer.lock are not served
//   * an unknown path goes to index.php (the front-controller convention of
//     Laravel, Slim and friends) when there is one, otherwise 404
$__venvy_front = (static function () {
    $deny = static function () {
        http_response_code(404);
        header('Content-Type: text/plain; charset=utf-8');
        echo "404 Not Found\n";
        return null;
    };

    $raw_req = (string)($_SERVER['REQUEST_URI'] ?? '/');
    $qpos = strpos($raw_req, '?');
    $raw_path = $qpos === false ? $raw_req : substr($raw_req, 0, $qpos);
    // Reject protocol-relative / double-slash or non-root paths: parse_url()
    // treats "//.env" as host ".env" with NULL path, which would otherwise
    // bypass checks and let `php -S` serve the original URI on `return false`.
    if ($raw_path === '' || $raw_path[0] !== '/' || strpos($raw_path, '//') === 0) {
        return $deny();
    }
    $uri = rawurldecode($raw_path);
    if (strpos($uri, "\0") !== false || strpos($uri, '//') === 0) {
        return $deny();
    }

    $docroot = realpath((string)($_SERVER['DOCUMENT_ROOT'] ?? ''));
    if ($docroot === false) {
        return $deny();
    }
    $cwd = realpath(getcwd() ?: '');
    $at_app_root = ($cwd !== false && $docroot === $cwd);

    $check_parts = static function (array $parts) use ($at_app_root) {
        foreach ($parts as $p) {
            if ($p === '' || $p === '..' || $p === '.') return false;
            if ($p[0] === '.' && $p !== '.well-known') return false;
        }
        if ($parts) {
            $first = strtolower($parts[0]);
            if ($at_app_root && ($first === 'logs' || $first === 'vendor')) return false;
            $last = strtolower($parts[count($parts) - 1]);
            if ($last === 'composer.json' || $last === 'composer.lock') return false;
        }
        return true;
    };

    $req_parts = array_values(array_filter(explode('/', $uri), 'strlen'));
    if (!$check_parts($req_parts)) {
        return $deny();
    }

    $check_resolved = static function ($real) use ($docroot, $check_parts) {
        if ($real === false) return false;
        if ($real === $docroot) return true;
        if (strpos($real, $docroot . DIRECTORY_SEPARATOR) !== 0) return false;
        $rel = substr($real, strlen($docroot) + 1);
        $rel_parts = array_values(array_filter(explode(DIRECTORY_SEPARATOR, $rel), 'strlen'));
        return $check_parts($rel_parts);
    };

    $path = realpath($docroot . $uri);
    if ($path !== false && $check_resolved($path)) {
        if (is_file($path)) {
            return false;
        }
        if (is_dir($path)) {
            foreach (['index.php', 'index.html', 'index.htm'] as $idx) {
                $cand = realpath($path . DIRECTORY_SEPARATOR . $idx);
                if ($cand !== false && is_file($cand) && $check_resolved($cand)) {
                    return false;
                }
            }
        }
    }

    $front = realpath($docroot . DIRECTORY_SEPARATOR . 'index.php');
    if ($front !== false && is_file($front) && $check_resolved($front)) {
        $_SERVER['SCRIPT_NAME'] = '/index.php';
        $_SERVER['SCRIPT_FILENAME'] = $front;
        $_SERVER['PHP_SELF'] = '/index.php';
        chdir($docroot);
        return $front;
    }

    return $deny();
})();

if ($__venvy_front === false) {
    return false;
}
if (is_string($__venvy_front)) {
    require $__venvy_front;
}
return true;
