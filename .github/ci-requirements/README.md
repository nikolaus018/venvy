# CI dependencies, locked with hashes

CI installs everything with `pip install --require-hashes`, so a package that
was swapped on PyPI after it was locked fails to install instead of running in
CI. Each `.txt` here is generated from its `.in`:

| File | For |
|---|---|
| `tests.txt` | the test suite: `requirements.txt` (Venvy's own pins) plus the test client |
| `audit.txt` | `pip-audit` |
| `checks.txt` | the checks against an installed Venvy (`tests/t_installed.py`) |

After changing `requirements.txt` (a Dependabot pull request, say) or a `.in`
file, regenerate them; CI fails until `tests.txt` has the same pins as
`requirements.txt`:

```bash
cd .github/ci-requirements
for n in tests audit checks; do uv pip compile $n.in --generate-hashes --universal --python-version 3.10 -o $n.txt; done
```
