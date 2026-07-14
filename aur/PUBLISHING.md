# Publishing to the AUR

Everything needed is in `aur/` — this is the checklist to go live.

## 1. Publish the GitHub repo first

```bash
git init && git add -A && git commit -m "Proton Command Center v1.1.0"
git remote add origin git@github.com:mrcgibb9876-hash/proton_command_center.git
git push -u origin main
git tag v1.1.0 && git push --tags
```

The PKGBUILDs already point at your repo
(github.com/mrcgibb9876-hash/proton_command_center) — no username
substitution needed. Just make sure the repo root contains `pcc.py`,
`index.html`, `README.md`, `LICENSE`, and the `packaging/` directory,
since the package installs from those paths.

## 2. Fill in real checksums and verify the build

```bash
cd aur/proton-command-center
updpkgsums                      # downloads the tag tarball, writes sha256
makepkg --printsrcinfo > .SRCINFO
makepkg -si                     # full local build + install test
namcap PKGBUILD *.pkg.tar.zst   # optional lint (pacman -S namcap)
```

The `-git` package needs no checksum work — just regenerate its .SRCINFO:

```bash
cd ../proton-command-center-git
makepkg --printsrcinfo > .SRCINFO
```

## 3. Create the AUR account and SSH key

Register at https://aur.archlinux.org, then add an SSH public key under
My Account. Test with `ssh aur@aur.archlinux.org` (it should greet you and
disconnect).

## 4. Push each package

The AUR creates a package repo the first time you push to its name:

```bash
git clone ssh://aur@aur.archlinux.org/proton-command-center.git aur-release
cp aur/proton-command-center/PKGBUILD aur/proton-command-center/.SRCINFO aur-release/
cd aur-release && git add PKGBUILD .SRCINFO
git commit -m "Initial release: v1.1.0" && git push
```

Repeat with `proton-command-center-git` for the VCS package.

## 5. After publishing

- Users install with `yay -S proton-command-center` (or paru, etc.) and
  enable the service: `systemctl --user enable --now proton-command-center`.
- New release flow: bump `pkgver` in the PKGBUILD, `git tag vX.Y.Z && git
  push --tags` on GitHub, then `updpkgsums`, regenerate `.SRCINFO`, commit,
  push to the AUR repo.
- Rules of the road: never commit built packages or `src/` to the AUR repo,
  always regenerate `.SRCINFO` on any PKGBUILD change (the AUR web UI reads
  it, not the PKGBUILD), and reply to comments on your package page — that's
  where users report issues.
