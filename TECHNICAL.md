# How AE Skinner works, and what it took to make it universal

Everything here was found by reverse-engineering shipping After Effects
installs (2020, 2024, and the 26.x Betas). None of it is documented by Adobe.

## Where the artwork and colours live

Both live as named PE resources inside `Support Files\AfterFXLib.dll`:

| Resource type | Names | What it is |
|---|---|---|
| `PNG` | `AE_SPLASH*`, `AE_BETA_SPLASH*`, `AE_ABOUT*` | Startup and About artwork, one per DPI scale |
| `XML` | `DVACOLORTHEMESV1`…`V6`, `AECOLORTHEMES`, `DESIGNAPPCOLORTHEMESV5`, `HIGHCONTRASTCOLORTHEMEV5` | Interface colour themes |
| `JSON` | `AE-SPECTRUM-COLORTHEMES` | Spectrum tokens (2024+) |

`dvaui.dll` carries a couple more (`SPECTRUM_DARK`, `SPECTRUM_LIGHT`).

They are replaced with `BeginUpdateResource` / `UpdateResource` /
`EndUpdateResource`, then the PE checksum is recomputed so Windows keeps
loading the binary.

## The four things that differ per version

Every naive patch script hardcodes all four. Each one silently breaks.

### 1. Resource language is not always 1033

| Install | Language ID |
|---|---|
| AE 2020 | `0` (neutral) |
| AE 2024 | `0` (neutral) |
| AE Beta 26.x | `1033` (en-US) |

This is the nastiest of the four, because **`UpdateResourceW` with the wrong
language does not fail.** It adds a *second* resource under the language you
asked for and leaves the original in place. AE keeps loading the original, the
patch appears to succeed, and nothing changes.

Enumerate the language actually present and write back to that.

### 2. Splash dimensions differ

| Install | 1x | 1.5x | 2x |
|---|---|---|---|
| AE 2020, 2024 | 750×500 | 1125×750 | 1500×1000 |
| AE Beta 26.x | 766×516 | 1149×774 | 1532×1032 |

Read the width and height out of each PNG's IHDR chunk and regenerate at that
exact size. Also reuse the original resource's **alpha channel** — that is
where AE's rounded corners live.

### 3. Grey ramp length differs

`DVACOLORTHEMESV5` uses ten stops, `kColor_Gray_01` through `kColor_Gray_10`.
`DVACOLORTHEMESV6` uses eleven, `SPECTRUM_GLOBAL_COLOR_GRAY_50` through
`GRAY_900`, on Adobe's Spectrum scale.

So a theme cannot be a fixed list of colours. AE Skinner declares one ramp and
resamples it to however many stops the resource actually has.

### 4. Stock stores greys with no hue at all

This is the one that defeats every "just edit the theme XML" answer online.

A **pristine** AE 2024 stores its dark-theme greys as an entity reference:

```xml
<KeyFrame name="&kColor_Gray_01;" v="&kColor_Gray_Darkest_01;" />
```

`&kColor_Gray_Darkest_01;` expands to a **brightness value only** — `0.0471`.
There is no hue and no saturation field to redefine. You can rewrite that
entity all day and the panel stays grey, because grey is what
`h=0, s=0, v=0.0471` means and there is nowhere to put a hue.

The fix is to replace the reference with an explicit triple:

```xml
<KeyFrame name="&kColor_Gray_01;" h="257.14" s="0.5833" v="0.0471" />
```

An install that has already been skinned is in the *second* form, so a tool
that only handles the first cannot re-skin it. Handle both.

## Adobe removed interface theming in AE 2025

On AE 2025 and later (major version ≥ 25) all of the above still works — the
resources patch, verify, and hold — and the panels stay grey anyway. The UI
toolkit no longer reads the hue.

Confirmed on Beta 25.6, 26.2, 26.3 and 26.5 by patching, verifying the bytes on
disk, and sampling the running application's pixels. Splash, About and sounds
are unaffected and still work on every version.

AE Skinner labels those installs `no theme` and warns before applying.

## Not touched, deliberately

Only chrome and the accent are recoloured. These are left alone by name:

* label colours, warnings, errors, cache and audio bars
* the Spectrum semantic families — `CELERY`, `CHARTREUSE`, `RED`, `YELLOW`,
  `ORANGE`, `MAGENTA`, `SEAFOAM`, `INDIGO`, `TURQUOISE`
* `HIGHCONTRASTCOLORTHEMEV5`, which is the accessibility theme
* every light theme, unless you ask for it

The accent rule only fires on hues in Adobe's blue band (190–232°) that are
saturated enough to be chrome rather than a washed-out grey.

## Performance note

`AfterFXLib.dll` is ~58 MB and carries about **2,400 PNG resources**. Reading
them all to find the dozen splash and About slots costs roughly 6 seconds.
Filtering on the resource *name* before fetching any bytes takes it to
**0.09 s** — an 80× difference, and the reason the UI does not freeze.
