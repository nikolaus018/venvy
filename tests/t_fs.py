"""File-manager behaviour around symlinks + simulated swap-after-check races.
usage: t_fs.py <repo>"""
import importlib.util as u
import os, sys, tempfile
from pathlib import Path
from harness import admin, make_app, server

repo = sys.argv[1]
fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name, extra if not cond else "")
    if not cond: fails.append(name)

with server(repo, 54999) as (base, data, srv):
    cl = admin(base)
    app_id = make_app(cl)
    app_dir = next((Path(data) / "apps").glob("*/*"))
    outside = Path(tempfile.mkdtemp(prefix="venvy-outside-"))
    (outside / "secret.txt").write_text("TOP SECRET")
    op = lambda **b: cl.req("POST", f"/api/apps/{app_id}/files/op", json=b)
    dl = lambda p: cl.req("GET", f"/api/apps/{app_id}/files/download", params={"path": p})

    check("mkdir nested", op(operation="create_folder", path="sub/deep").status_code == 200 and (app_dir / "sub/deep").is_dir())
    check("create file w/ parents", op(operation="create_file", path="a/b/c.txt").status_code == 200 and (app_dir / "a/b/c.txt").is_file())
    check("save new", op(operation="save", path="sub/f.txt", content="one").status_code == 200)
    os.chmod(app_dir / "sub/f.txt", 0o755)
    check("save keeps mode", op(operation="save", path="sub/f.txt", content="two").status_code == 200
          and (app_dir / "sub/f.txt").read_text() == "two" and (os.stat(app_dir / "sub/f.txt").st_mode & 0o777) == 0o755)
    check("save unicode", op(operation="save", path="sub/ü.txt", content="héllo ✓").status_code == 200 and dl("sub/ü.txt").text == "héllo ✓")
    r = dl("sub/f.txt")
    check("download headers", r.status_code == 200 and r.headers["content-length"] == "3" and "attachment" in r.headers["content-disposition"], str(dict(r.headers)))
    check("download dir -> 404", dl("sub").status_code == 404)
    check("download missing -> 404", dl("nope.txt").status_code == 404)
    check("save onto folder -> 409", op(operation="save", path="sub", content="x").status_code == 409)
    check("no leftover temp files", not [p for p in (app_dir / "sub").iterdir() if ".venvy-" in p.name])

    os.symlink(outside, app_dir / "link_out")
    os.symlink("sub", app_dir / "link_in")
    check("read through outside link refused", dl("link_out/secret.txt").status_code == 403)
    check("write through outside link refused", op(operation="save", path="link_out/pwn.txt", content="x").status_code == 403 and not (outside / "pwn.txt").exists())
    check("mkdir through outside link refused", op(operation="create_folder", path="link_out/d").status_code == 403 and not (outside / "d").exists())
    check("legit inside link still works", op(operation="save", path="link_in/via.txt", content="v").status_code == 200 and (app_dir / "sub/via.txt").read_text() == "v")
    check("read via inside link", dl("link_in/f.txt").text == "two")
    tree = cl.req("GET", f"/api/apps/{app_id}/files/tree", params={"path": "link_in"}).json()
    check("tree via inside link", any(f["name"] == "f.txt" and f["path"] == "sub/f.txt" for f in tree), str(tree))
    check("tree of outside link refused", cl.req("GET", f"/api/apps/{app_id}/files/tree", params={"path": "link_out"}).status_code == 403)

    check("copy file", op(operation="copy", path="sub/f.txt", new_path="sub/f2.txt").status_code == 200 and (app_dir / "sub/f2.txt").read_text() == "two")
    check("copy onto existing -> 409", op(operation="copy", path="sub/f.txt", new_path="sub/f2.txt").status_code == 409)
    os.symlink(outside / "secret.txt", app_dir / "sub/leak")
    check("copy dir", op(operation="copy", path="sub", new_path="sub_copy").status_code == 200
          and (app_dir / "sub_copy/f.txt").read_text() == "two" and (app_dir / "sub_copy/deep").is_dir())
    check("copy keeps symlinks as symlinks", (app_dir / "sub_copy/leak").is_symlink())
    check("rename", op(operation="rename", path="sub_copy", new_path="moved/here").status_code == 200 and (app_dir / "moved/here/f.txt").is_file())
    check("rename onto existing -> 409", op(operation="rename", path="sub/f.txt", new_path="sub/f2.txt").status_code == 409)
    check("rename missing -> 404", op(operation="rename", path="ghost", new_path="g2").status_code == 404)

    check("delete inside link removes the link only", op(operation="delete", path="link_in").status_code == 200
          and not (app_dir / "link_in").is_symlink() and (app_dir / "sub/f.txt").exists())
    check("delete outside link removes the link only", op(operation="delete", path="link_out").status_code == 200
          and not os.path.lexists(app_dir / "link_out") and (outside / "secret.txt").exists())
    os.symlink(outside, app_dir / "moved/here/out2")
    check("delete tree does not descend through links", op(operation="delete", path="moved").status_code == 200
          and not (app_dir / "moved").exists() and (outside / "secret.txt").exists())
    check("delete root refused", op(operation="delete", path="").status_code == 400 and app_dir.is_dir())
    check("escape refused", op(operation="delete", path="../../data.db").status_code == 403 and (Path(data) / "data.db").exists())
    check("rename out refused", op(operation="rename", path="sub/f.txt", new_path="../../x").status_code == 403)

    # chunked + single-request uploads
    r = cl.req("POST", f"/api/apps/{app_id}/files/upload/chunk", files={"chunk": ("c", b"abc")},
               data={"upload_id": "cd" * 16, "path": "sub", "rel_path": "new/dir/up.bin", "offset": "0", "total": "3"})
    check("chunk upload into new dirs", r.status_code == 200 and (app_dir / "sub/new/dir/up.bin").read_bytes() == b"abc", r.text)
    r = cl.req("POST", f"/api/apps/{app_id}/files/upload/chunk", files={"chunk": ("c", b"abc")},
               data={"upload_id": "ce" * 16, "path": "", "rel_path": "sub", "offset": "0", "total": "3"})
    check("chunk upload onto folder -> 409", r.status_code == 409, r.text)
    r = cl.req("POST", f"/api/apps/{app_id}/files/upload", data={"path": "sub"},
               files=[("files", ("multi/a.txt", b"A")), ("files", ("../evil.txt", b"E")), ("files", ("b.txt", b"B"))])
    j = r.json()
    check("single-request upload", r.status_code == 200 and (app_dir / "sub/multi/a.txt").read_bytes() == b"A"
          and (app_dir / "sub/b.txt").read_bytes() == b"B" and [s["path"] for s in j["skipped"]] == ["../evil.txt"], r.text)

# --- swap-after-check: call the fs layer with components that have become symlinks
work = Path(tempfile.mkdtemp(prefix="venvy-race-")); os.chdir(work)
sys.path.insert(0, os.path.abspath(repo))
import venvy.safefs as fs
import main as iso
root = work / "app"; (root / "real").mkdir(parents=True)
victim = work / "victim"; victim.mkdir(); (victim / "keep.txt").write_text("keep")
os.symlink(victim, root / "d")          # "d" was a directory when the path was checked
def refused(fn, *a):
    try: fn(*a); return False
    except fs.AppPathError: return True
check("race: save refused", refused(fs._fs_save, root, ("d", "x.txt"), b"x", None) and not (victim / "x.txt").exists())
check("race: touch refused", refused(fs._fs_touch, root, ("d", "t"), None) and not (victim / "t").exists())
check("race: mkdir refused", refused(fs._fs_mkdir, root, ("d", "nd"), None) and not (victim / "nd").exists())
check("race: delete refused", refused(fs._fs_delete, root, ("d", "keep.txt")) and (victim / "keep.txt").exists())
check("race: read refused", refused(fs._fs_open_file, root, ("d", "keep.txt")))
check("race: move-out refused", refused(fs._fs_move_or_copy, root, ("real",), ("d", "moved"), False, None, "x") and not (victim / "moved").exists())
(work / "part").write_bytes(b"p")
check("race: install refused", refused(fs._fs_install, root, ("d", "p.bin"), work / "part", None) and not (victim / "p.bin").exists())
os.symlink(victim / "keep.txt", root / "real" / "f")    # final component swapped for a link
fs._fs_save(root, ("real", "f"), b"new", None)
check("race: save replaces a link, never writes through it", (victim / "keep.txt").read_text() == "keep" and (root / "real/f").read_text() == "new" and not (root / "real/f").is_symlink())
os.symlink(victim / "keep.txt", root / "real" / "g")
check("race: read of link refused", refused(fs._fs_open_file, root, ("real", "g")))
iso._chown_tree(root, os.getuid(), os.getgid())
print("FAILED:" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
