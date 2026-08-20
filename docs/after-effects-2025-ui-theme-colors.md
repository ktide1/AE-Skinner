# Why After Effects 2025 and 2026 ignore UI theme colors

**Short answer: Adobe removed it.** If you used to recolor the After Effects
interface and it stopped working when you upgraded, you did not break anything
and there is no setting you missed. The 2025 release quietly dropped support
for colorizing the UI, and it has not come back in 2026 or the Betas.

This page documents the evidence, because as far as I can tell nobody had
written it down.

## Symptoms

You are probably here because one of these happened:

- You set **Preferences → Appearance** and only the brightness changes — the
  panels stay gray.
- `Enable_Theme_Colorizing` in `Debug Database.txt` used to tint the interface
  and now does nothing.
- A script, patch, or tutorial that recolored `AfterFXLib.dll` worked on AE
  2024 or earlier and produces no visible change on 2025+.
- You edited the theme XML by hand, the file is definitely changed, and After
  Effects still looks the same.

Everything above has the same cause.

## Where the colors live

The interface palette is stored as named resources inside
`Support Files\AfterFXLib.dll`:

| Resource | What it holds |
|---|---|
| `DVACOLORTHEMESV1` … `DVACOLORTHEMESV6` | The shared Adobe UI themes |
| `AECOLORTHEMES` | After Effects specific colors |
| `DESIGNAPPCOLORTHEMESV5` | Design app palette |
| `AE-SPECTRUM-COLORTHEMES` (JSON) | Spectrum tokens, 2024 and later |
| `HIGHCONTRASTCOLORTHEMEV5` | The accessibility theme |

`dvaui.dll` carries a couple more. Each holds `<KeyFrame>` entries with hue,
saturation and value attributes per theme — `Darkest`, `Dark`, `Middark`,
`Light`, and so on.

## What was tested

On this machine: AE 2020 (17.0), AE 2024 (24.5), and Betas 25.6, 26.2, 26.3
and 26.5.

For each install the theme resources were rewritten with a purple palette, the
PE checksum recalculated, and the result **read back out of the binary and
compared byte for byte** to confirm the patch actually landed. Preferences were
set to darkest brightness and `Enable_Theme_Colorizing` set to `true`.

| Version | Resources patch and verify | Interface actually changes |
|---|---|---|
| AE 2020 (17.0) | yes | **yes** |
| AE 2024 (24.5) | yes | **yes** |
| AE Beta 25.6 | yes | no |
| AE Beta 26.2 | yes | no |
| AE Beta 26.3 | yes | no |
| AE Beta 26.5 | yes | no |

On 2025 and later the bytes on disk are unambiguously different, and the
running application samples out at the same neutral gray as a stock install.
The resources are still loaded — the toolkit simply no longer applies their
hue. Splash art, the About window, and the notification sounds are unaffected
and still work on every version.

This lines up with what users have been reporting on the Adobe forums since the
2025 release: [UI theme color customization — Adobe Community](https://community.adobe.com/feature-requests-530/ui-theme-color-customization-1548801).

## The thing that trips everyone up on older versions

Worth writing down separately, because it defeats most "just edit the theme
XML" advice even on versions where theming *does* work.

A stock After Effects stores its dark grays as an **entity reference with no
hue at all**:

```xml
<KeyFrame name="&kColor_Gray_01;" v="&kColor_Gray_Darkest_01;" />
```

`&kColor_Gray_Darkest_01;` expands to a brightness value only — `0.0471`. There
is no hue field and no saturation field. You can redefine that entity forever
and the panel stays gray, because gray is exactly what a lone `v` means.

To get a color in, the reference has to be replaced with an explicit triple:

```xml
<KeyFrame name="&kColor_Gray_01;" h="257.14" s="0.5833" v="0.0471" />
```

An install that has already been recolored is in the second form, so a tool
that only understands the first cannot re-skin it. Both forms have to be
handled.

## What you can still do

On **AE 2024 and older**, full interface recoloring works.

On **AE 2025, 2026 and the Betas**, you can still replace:

- the startup splash screen
- the About window artwork
- the render-finished and render-failed sounds

You can also still set overall UI brightness, label colors, and guide colors
through normal Preferences. It is the panel *hue* that is gone.

## A tool that does all of this

[AE Skinner](https://github.com/ktide1/AE-Skinner) — free, MIT licensed,
Windows. It detects every After Effects install on the machine, tells you which
ones can still be themed, backs everything up before it writes, and restores
with one click.

It is not affiliated with Adobe and contains no Adobe code; it rewrites
resources in the copy of After Effects already installed on your machine, and
touches nothing related to licensing or activation.
