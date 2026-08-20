# Third-party notices

AE Skinner is MIT licensed (see `LICENSE`). It depends on, and the packaged
`.exe` builds bundle, the following third-party software. Each remains under
its own licence.

| Component | Licence | Project |
|---|---|---|
| Pillow | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| pefile | MIT | https://github.com/erocarrera/pefile |
| tkinterdnd2 | MIT | https://github.com/pmgagne/tkinterdnd2 |
| tkdnd | BSD-style (bundled by tkinterdnd2) | https://github.com/petasis/tkdnd |
| Python | PSF License 2.0 | https://docs.python.org/3/license.html |
| Tcl/Tk | BSD-style | https://www.tcl.tk/software/tcltk/license.html |
| PyInstaller (build tool) | GPL 2.0+ with bootloader exception | https://pyinstaller.org/en/stable/license.html |

## About the PyInstaller licence

The packaged `AE Skinner.exe` and `aeskin.exe` are produced with PyInstaller.

PyInstaller is distributed under GPL 2.0 **with an explicit exception covering
the bootloader**, which is the part that ends up inside the produced
executable. That exception is what allows applications built with PyInstaller
to be released under any licence the author chooses, including proprietary
ones. From the PyInstaller licence page: the executables it generates from your
source code "can be shipped with whatever license you want."

The exception covers *using* PyInstaller. Modifications to PyInstaller's own
source remain under the GPL. AE Skinner does not modify PyInstaller.

Source: https://pyinstaller.org/en/stable/license.html

## Optional runtime dependency

`ffmpeg` is used, if it is present on `PATH`, to convert audio into the PCM WAV
format After Effects expects. It is never bundled and never downloaded — AE
Skinner only invokes a copy you already installed. Without it, sound
replacement still works when given a 16-bit PCM `.wav`.

ffmpeg is licensed under the LGPL 2.1+ or GPL 2+ depending on build options:
https://ffmpeg.org/legal.html

## Fonts

No fonts are bundled. Captions are rendered with fonts already installed on the
machine (Segoe UI or Arial by default), or with a `.ttf` you point at.
