"""Every font file shipped in the repository is named in THIRD-PARTY-LICENSES
with its license, and its license text ships next to it. The repository states
that it vendors nothing else; a font added without this would make that false."""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_every_shipped_font_is_attributed():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    fonts = [f for f in tracked if f.endswith((".woff", ".woff2", ".ttf", ".otf"))]
    notices = (ROOT / "THIRD-PARTY-LICENSES").read_text()
    assert fonts, "this test is for the dashboard's fonts; update it if they are gone"
    for font in fonts:
        assert font in notices, f"{font} is not listed in THIRD-PARTY-LICENSES"
        licences = list((ROOT / font).parent.glob("OFL-*.txt"))
        assert licences, f"no license text next to {font}"
    assert "SIL Open Font License" in notices
