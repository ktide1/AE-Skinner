# Notice and disclaimer

**AE Skinner is not affiliated with, endorsed by, sponsored by, or connected to
Adobe Inc.** "Adobe", "After Effects" and "Creative Cloud" are trademarks of
Adobe Inc., used here only to describe what this software is compatible with.

## What this software contains

AE Skinner contains **no Adobe code, artwork, or assets of any kind.** It is a
patcher. It reads resources out of the copy of After Effects already installed
on the machine it runs on, rewrites those resources in place, and writes a
backup of the originals first.

Nothing in this project circumvents, disables, or interferes with licensing,
activation, digital rights management, or any other technical protection
measure. The changes it makes are cosmetic: artwork, interface colours, and
notification sounds.

## What you are responsible for

Modifying an installed application may conflict with the licence agreement you
accepted for that application. Whether to do so is your decision, and the
consequences are yours. Read your Adobe licence terms.

The authors provide this software as-is, with no warranty, and accept no
liability for damage to an installation, lost work, or any other outcome. See
`LICENSE`.

## Do not redistribute patched binaries

You may share AE Skinner freely under the MIT licence.

**Do not distribute a patched `AfterFXLib.dll`, `dvaui.dll`, or any other Adobe
file, patched or otherwise.** That would be redistributing Adobe's copyrighted
work, which this project's licence does not and cannot permit.

## Practical notes

* A Creative Cloud update will replace the patched binaries and revert your
  skin. This is expected.
* Adobe support will reasonably decline to help with a modified installation.
  Restore from a backup before reporting a bug to Adobe.
* Backups live in `%LOCALAPPDATA%\AESkinnerackups`, outside the Adobe
  folder, so an update cannot delete them.
