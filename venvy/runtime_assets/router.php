<?php
// Router behind Venvy's "PHP" runtime, used with `php -S` when the app names
// no router script of its own. Files are served by PHP itself (return false:
// .php files run, everything else is sent as it is). What this adds:
//   * dotfiles, dot-folders (except .well-known) and Venvy's logs/ are never served
//   * composer.json / composer.lock are not served
//   * an unknown path goes to index.php (the front-controller convention of
//     Laravel, Slim and friends) when there is one, otherwise 404
$uri   = rawurldecode(parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH) ?: '/');
$parts = array_values(array_filter(explode('/', $uri), 'strlen'));

function venvy_deny() {
    http_response_code(404);
    header('Content-Type: text/plain; charset=utf-8');
    echo "404 Not Found\n";
    return true;
}

foreach ($parts as $p) {
    if ($p[0] === '.' && $p !== '.well-known') return venvy_deny();
    if ($p === '..') return venvy_deny();
}
$docroot = realpath($_SERVER['DOCUMENT_ROOT']);
if ($docroot === realpath(getcwd()) && $parts && $parts[0] === 'logs') return venvy_deny();
if (in_array(strtolower(basename($uri)), ['composer.json', 'composer.lock'], true)) return venvy_deny();

$path = realpath($docroot . $uri);
if ($path !== false && ($path === $docroot || strpos($path, $docroot . DIRECTORY_SEPARATOR) === 0)) {
    if (is_file($path)) return false;
    if (is_dir($path)) {
        foreach (['index.php', 'index.html', 'index.htm'] as $i) {
            if (is_file($path . DIRECTORY_SEPARATOR . $i)) return false;
        }
    }
}
$front = $docroot . DIRECTORY_SEPARATOR . 'index.php';
if (is_file($front)) {
    $_SERVER['SCRIPT_NAME'] = '/index.php';
    $_SERVER['SCRIPT_FILENAME'] = $front;
    $_SERVER['PHP_SELF'] = '/index.php';
    chdir($docroot);
    require $front;
    return true;
}
return venvy_deny();
