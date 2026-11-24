# UI fonts

Served by the dashboard backend at `/fonts/` (ConfigMap
`cluster-dashboard-fonts`), so the UI makes no request outside the
dashboard's origin. Both families are licensed under the SIL Open Font
License 1.1 (`OFL-*.txt`, served next to the fonts).

| File | Source | SHA-256 |
|---|---|---|
| `manrope-latin.woff2` | Manrope, latin subset, variable (weights 400 to 800), as served by Google Fonts: `https://fonts.gstatic.com/s/manrope/v23/xn7gYHE41ni1AdIRggexSvfedN4.woff2` | `8bd97e6bbbc1f602a8f791530eed1064c9a357d5731bbc76e3fc587fb068728f` |
| `jetbrains-mono-latin.woff2` | JetBrains Mono, latin subset, variable (weights 400 to 700), as served by Google Fonts: `https://fonts.gstatic.com/s/jetbrainsmono/v24/tDbv2o-flEEny0FZhsfKu5WU4zr3E_BX0PnT8RD8yKwBNntkaToggR7BYRbKPxDcwgknk-4.woff2` | `2c32b9b3ee358c119e210f6f5195f9bd34894d78a785ff2e95d60e718e400af4` |
| `OFL-Manrope.txt` | `https://raw.githubusercontent.com/google/fonts/main/ofl/manrope/OFL.txt` | `58172e0c0fac2cda8a37b348164bb55e44b0e69051e557e92b1d3f6910141f7b` |
| `OFL-JetBrainsMono.txt` | `https://raw.githubusercontent.com/google/fonts/main/ofl/jetbrainsmono/OFL.txt` | `b2fe5e8987594e9ffd1d2ca52a2f5d73eb8335243893c5d6254b5ad69269591d` |

To add a font, add the file here, to the `cluster-dashboard-fonts` generator
in `kustomization.yaml` and an `@font-face` rule in `cluster-topology.html`;
the backend serves every `*.woff2` and `*.txt` file it finds in the mount.
